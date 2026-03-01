from openai import OpenAI
from flask import request, jsonify
import json
import ast
#------------------------------------------------------------------
import os
import mysql.connector
from datetime import datetime, timezone
import phase_2_queries
import re


def get_ollama_client():
    return OpenAI(
        base_url="http://100.91.71.61:11434/v1",
        api_key="ollama"  # dummy value required by SDK
    )

def get_deepseek_client():
    return OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com/v1"
    )

def get_xai_client():
    return OpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )

#------------------------------------------------------------------
def connect()->object:
    return mysql.connector.connect(
        user=os.getenv('DB_USER'), 
        password=os.getenv('DB_PASSWORD'), 
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST', 'localhost') )
#------------------------------------------------------------------
def getResponseStream(prompt, current_scene, player_name, client):
    client = get_deepseek_client()
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            temperature=0.85,
            top_p=0.9,
            stream=True, 
            messages=[
                {
                    "role": "system",
                    "content": prompt
                },
                {
                    "role": "user",
                    "content": f"""
                        CURRENT SCENE FOR REFERENCE ONLY
                        -------------
                        {current_scene}

                        PLAYER NAME
                        -----------
                        {player_name}
                        """
                }
            ],
        )

        full = []

        for chunk in response:
            delta = chunk.choices[0].delta

            if delta and delta.content:
                token = delta.content
                full.append(token)
                yield token
        return "".join(full)

    except Exception as e:
        print("ERROR:", e)
#------------------------------------------------------------------
def classify_player_input(player_text: str, raw_mem: str, client, idNPC: int, idUser: int):
    client = get_deepseek_client()

    print(f"\nCLASSIFIER INPUT: {player_text}\n")

    # -----------------------------------
    # Build memory context
    # -----------------------------------
    mem_context = get_most_recent_scene(raw_mem)
    db = connect()
    cursor = db.cursor(dictionary=True)
    # -----------------------------------
    # Fetch rcent dialogue
    # -----------------------------------
    cursor.execute("""
            SELECT playerText, npcText
            FROM npc_user_memory_buffer
            WHERE idNPC = %s
            AND idUser = %s
            AND processed = 0
            ORDER BY createdAt ASC
        """, (idNPC, idUser))

    rows = cursor.fetchall()

    recent_dialogue_lines = []

    for r in rows:
        if r.get("playerText"):
            recent_dialogue_lines.append(f"Player: {r['playerText']}")
        if r.get("npcText"):
            recent_dialogue_lines.append(f"You: {r['npcText']}")

    recent_dialogue = "\n".join(recent_dialogue_lines)
    # -----------------------------------
    # Fetch NPC persona + beliefs
    # -----------------------------------

    cursor.execute("""
        SELECT p.personality_traits,
               p.emotional_tendencies,
               p.moral_alignment,
               p.role
        FROM npc_persona p
        WHERE p.idNPC = %s
    """, (idNPC,))
    persona = cursor.fetchone()

    cursor.execute("""
        SELECT trust
        FROM playerNPCrelationship
        WHERE idNPC = %s AND idUser = %s
    """, (idNPC, idUser))
    rel = cursor.fetchone()
    trust = rel["trust"] if rel else 50

    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_user_belief
        WHERE idNPC = %s AND idUser = %s
        AND confidence >= 0.3
    """, (idNPC, idUser))

    beliefs = cursor.fetchall()

    # Format beliefs
    belief_text = ""
    if beliefs:
        belief_text += "\nCurrent beliefs about the player:\n"
        for b in beliefs:
            belief_text += f"- {b['beliefType']}: {b['beliefValue']} (confidence {round(b['confidence'],2)})\n"


    # -----------------------------------
    # SELF BELIEFS (NPC about itself)
    # -----------------------------------
    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_self_belief
        WHERE idNPC=%s
        AND confidence >= 0.3
        ORDER BY beliefType, confidence DESC
    """, (idNPC,))

    self_beliefs = cursor.fetchall()

    self_belief_text = ""
    if self_beliefs:
        self_belief_text += "\nCore beliefs about self:\n"
        for b in self_beliefs:
            self_belief_text += (
                f"- {b['beliefType']}: "
                f"{b['beliefValue']} "
                f"(confidence {round(b['confidence'],2)})\n"
        )
            
    cursor.close()
    db.close()

    # -----------------------------------
    # SYSTEM MESSAGE
    # -----------------------------------
    system = f"""
    You are classifying player dialogue from the perspective of THIS NPC.

    NPC Role: {persona.get('role') if persona else None}
    Personality: {persona.get('personality_traits') if persona else None}
    Emotional tendencies: {persona.get('emotional_tendencies') if persona else None}
    Moral alignment: {persona.get('moral_alignment') if persona else None}
    Trust toward player: {trust}

    Interpretation Rules:
    - Interpret tone as THIS NPC would perceive it.
    - High trust → more generous interpretation.
    - Low trust → more suspicious interpretation.
    - Personality biases perception.
    - Existing beliefs influence emotional reading.
    {belief_text}
    {self_belief_text}

    Return ONLY valid JSON.
    Do not explain.
    Follow schema exactly.
    If no emotion clearly fits, use 'calm'.
    Do NOT invent new labels.
    """


    system += f"\nRecent interaction summary:\n{mem_context}\n\n{recent_dialogue}\n"

    # -----------------------------------
    # USER MESSAGE
    # -----------------------------------
    user = f"""
    Player text:
    \"\"\"{player_text}\"\"\"

    IMPORTANT:

    Determine whether the emotional tone is directed at:
    - the NPC
    - the player themself
    - the environment/situation
    - or no one in particular

    Classify with EXACTLY these fields:

    - sentiment: one of [positive, neutral, negative, hostile, affectionate]
    - intensity: number from 0.0 to 1.0
    - offensive: true or false
    - emotion: one of [happy, sad, angry, afraid, calm, excited, disgusted]
    - target: one of [npc, self, environment, none]

    Return JSON ONLY.
    """

    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    result = _safe_json_from_model(
        resp,
        fallback={
            "sentiment": "neutral",
            "intensity": 0.3,
            "offensive": False,
            "emotion": "calm",
            "target": "none",
            "trust_delta": 0
        }
    )

    # ----------------------------
    # TRUST ENGINE (deterministic)
    # ----------------------------
    target = result.get("target")
    sentiment = result.get("sentiment")
    offensive = result.get("offensive")
    intensity = float(result.get("intensity", 0.0))

    trust_delta = 0

    if offensive and target == "npc":
        trust_delta = -5

    elif sentiment == "hostile" and target == "npc":
        trust_delta = -3 

    elif sentiment == "negative" and target == "self":
        trust_delta = int(1 + intensity * 2)

    elif sentiment == "affectionate":
        trust_delta = int(1 + intensity * 3)

    elif sentiment == "positive" and target == "npc":
        trust_delta = int(1 + intensity * 2)

    result["trust_delta"] = trust_delta

    # Emotion validation
    ALLOWED_EMOTIONS = {
        "happy", "sad", "angry", "afraid", "calm", "excited", "disgusted"
    }

    if result.get("emotion") not in ALLOWED_EMOTIONS:
        result["emotion"] = "calm"


    # update database to record categorizations 
    record_classification_stats(idUser, idNPC, player_text, result)


    print(f"\nPLAYER INPUT CLASSIFIED:\n\{result}n")

    return result
#------------------------------------------------------------------
def extract_persona_clues(player_text: str, recent_context: dict, client, idNPC, idUser):
    client = get_deepseek_client()

    system = ""

    db = connect()
    cursor = db.cursor(dictionary=True)

    # NPC + persona
    cursor.execute("""
        SELECT n.age, n.gender,
            p.personality_traits,
            p.emotional_tendencies,
            p.moral_alignment,
            p.role
        FROM NPC n
        LEFT JOIN npc_persona p ON p.idNPC = n.idNPC
        WHERE n.idNPC = %s
    """, (idNPC,))
    npc = cursor.fetchone()

    # Relationship + trust
    cursor.execute("""
        SELECT trust
        FROM playerNPCrelationship
        WHERE idNPC = %s AND idUser = %s
    """, (idNPC, idUser))
    relationship = cursor.fetchone()
    trust = relationship["trust"] if relationship else 50

    # Dominant emotion
    cursor.execute("""
        SELECT e.emotion
        FROM npcEmotion ne
        JOIN emotion e ON e.idEmotion = ne.idEmotion
        WHERE ne.idNPC = %s
        ORDER BY ne.emotionIntensity DESC
        LIMIT 1
    """, (idNPC,))
    emotion_row = cursor.fetchone()
    npc_emotion = emotion_row["emotion"] if emotion_row else None


    # Existing beliefs about player
    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_user_belief
        WHERE idNPC=%s AND idUser=%s
    """, (idNPC, idUser))

    existing_beliefs = cursor.fetchall()

    # Existing beliefs about self
    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_self_belief
        WHERE idNPC=%s
        AND confidence >= 0.3
        ORDER BY beliefType, confidence DESC
    """, (idNPC,))

    self_beliefs = cursor.fetchall()

    if self_beliefs:
        system += "\nCore beliefs about self:\n"
        for row in self_beliefs:
            system += (
                f"- {row['beliefType']}: "
                f"{row['beliefValue']} "
                f"(confidence {round(row['confidence'],2)})\n"
            )

    belief_summary = {}
    for row in existing_beliefs:
        belief_summary.setdefault(row["beliefType"], [])
        belief_summary[row["beliefType"]].append(
            f"{row['beliefValue']} (confidence {round(row['confidence'],2)})"
        )

    if belief_summary:
        system += "\nCurrent beliefs about the player:\n"
        for btype, values in belief_summary.items():
            system += f"{btype}:\n"
            for v in values:
                system += f"- {v}\n"

    cursor.close()
    db.close()

    system += """

        Belief Revision Rules:
        ----------------------
        - Treat the current beliefs above as your existing working model.
        - New evidence may reinforce, weaken, or contradict them.
        - Do not discard specific high-confidence beliefs without strong evidence.
        - Prefer updating confidence over replacing specific facts with vague descriptions.
        - If new information is ambiguous, you may return null for that field.
        """

    system += f"""

        You are simulating how THIS NPC forms beliefs about a player.

        NPC PROFILE
        -----------
        Age: {npc['age']}
        Gender: {npc['gender']}
        Role: {npc['role']}
        Personality traits: {npc['personality_traits']}
        Emotional tendencies: {npc['emotional_tendencies']}
        Moral alignment: {npc['moral_alignment']}
        Current dominant emotion: {npc_emotion}
        Trust toward player: {trust}

        Inference Constraints:
        - Beliefs must reflect this NPC's maturity level.
        - A young child cannot infer complex adult psychology.
        - Personality biases interpretation.
        - High trust → generous interpretations.
        - Low trust → suspicious interpretations.
        - Current emotion influences interpretation.
        - Do NOT reason as an omniscient narrator.

        Return ONLY valid JSON.
        
        """

    past_memory, current_scene, _ = get_most_recent_scene(recent_context or "")

   
    system += (
        "\nRecent interaction summary (may provide behavioral patterns):\n"
        f"{current_scene}\n"
    )

    user = f"""
    Player text:
    \"\"\"{player_text}\"\"\"

    Form beliefs about the player.

    Return JSON with EXACTLY these fields:

    {{
        "current_emotion": {{ "value": string, "confidence": float }} or null,
        "moral_alignment": {{ "value": string, "confidence": float }} or null,
        "age": {{ "value": string, "confidence": float }} or null,
        "gender": {{ "value": string, "confidence": float }} or null,
        "life_story": {{ "value": string, "confidence": float }} or null,

        "personality_traits": [
            {{ "value": string, "confidence": float }}
        ],

        "secrets": [
            {{ "value": string, "confidence": float }}
        ],

        "goals": [
            {{ "value": string, "confidence": float }}
        ],

        "likes": [
            {{ "value": string, "confidence": float }}
        ],

        "dislikes": [
            {{ "value": string, "confidence": float }}
        ]
    }}

    Confidence Rules:
    - Confidence must be between 0.0 and 1.0.
    - 0.9+ = explicit or strongly supported.
    - 0.6–0.8 = strongly implied.
    - 0.3–0.5 = weak inference.
    - Below 0.3 = do not include.
    - Never output random confidence.
    - If uncertain, return null instead.
    - No extra keys.


    Inference Guidelines:

    - All fields may be inferred from tone, behavior, word choice, patterns, or contradictions.
    - Explicit claims should not automatically override behavioral evidence.
    - If a player claims one emotion but language strongly suggests another,
        choose the more strongly supported belief.
    - Age may be infered from style of speech, life_story, likes and dislikes, or statements made by player direclty about age.
     -Gender may be inferred from any relevant contextual evidence.
    - personality_traits should reflect enduring patterns.
    - goals may be inferred from expressed desires or recurring motivations.
    - secrets may be inferred if the player implies concealment or avoidance.
    - life_story may be inferred if background context is strongly suggested.
    - likes should reflect activities, topics, or experiences the player shows enthusiasm toward.
    - dislikes should reflect aversions, discomfort, or negative recurring themes.
    - Do not infer likes/dislikes from a single neutral statement.
    - Preferences should feel semi-stable, not momentary.
    - beliefValue must represent a SINGLE normalized trait or fact.
    - Do NOT include explanations or compound sentences.
    - Do NOT restate background context.
    - Avoid semantic duplicates of existing beliefs.
    - If a belief already exists in similar form, reinforce it instead of rephrasing it.
    - Prefer short canonical labels (e.g., figure_drawing_model, teacher_at_st_marcus).
    - Do NOT guess randomly.
    - No extra keys.
    
    """

    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    result = _safe_json_from_model(
    resp,
    fallback={
        "current_emotion": None,
        "moral_alignment": None,
        "age": None,
        "gender": None,
        "life_story": None,
        "personality_traits": [],
        "secrets": [],
        "goals": [],
        "likes": [],
        "dislikes": []
    }
    )

    # ------------------------------
    # Safety normalization
    # ------------------------------

    expected_lists = [
        "personality_traits",
        "secrets",
        "goals",
        "likes",
        "dislikes"
    ]

    expected_nullable = [
        "current_emotion",
        "moral_alignment",
        "age",
        "gender",
        "life_story"
    ]
      
    for key in expected_nullable:
        result[key] = normalize_object_field(result.get(key))

    for key in expected_lists:
        result[key] = normalize_list_field(result.get(key))

    seen = set()
    deduped = []

    for belief in result["personality_traits"]:
        canon = canonicalize(belief["value"])
        if canon not in seen:
            belief["value"] = canon
            deduped.append(belief)
            seen.add(canon)

    result["personality_traits"] = deduped

    return result

#------------------------------------------------------------------
def canonicalize(value: str) -> str:
    return (
        value.lower()
        .replace("has experience being a ", "")
        .replace("has experience as a ", "")
        .replace("is comfortable with ", "")
        .replace("likely has ", "")
        .replace("has lived in this town their whole life", "local_resident")
        .strip()
    )

#------------------------------------------------------------------
def extract_self_beliefs(npc_output: str, recent_context: dict, client, idNPC: int):
    client = get_deepseek_client()

    db = connect()
    cursor = db.cursor(dictionary=True)

    # ----------------------------------------
    # Core NPC profile (stable identity seed)
    # ----------------------------------------
    cursor.execute("""
        SELECT n.age, n.gender,
               p.role,
               p.personality_traits,
               p.emotional_tendencies,
               p.moral_alignment
        FROM NPC n
        LEFT JOIN npc_persona p ON p.idNPC = n.idNPC
        WHERE n.idNPC = %s
    """, (idNPC,))
    npc = cursor.fetchone()

    # ----------------------------------------
    # Current dominant emotion
    # ----------------------------------------
    cursor.execute("""
        SELECT e.emotion
        FROM npcEmotion ne
        JOIN emotion e ON e.idEmotion = ne.idEmotion
        WHERE ne.idNPC = %s
        ORDER BY ne.emotionIntensity DESC
        LIMIT 1
    """, (idNPC,))
    emotion_row = cursor.fetchone()
    npc_emotion = emotion_row["emotion"] if emotion_row else None

    # ----------------------------------------
    # Existing self beliefs
    # ----------------------------------------
    cursor.execute("""
        SELECT beliefType, beliefValue, confidence, stability
        FROM npc_self_belief
        WHERE idNPC = %s
    """, (idNPC,))
    existing = cursor.fetchall()

    cursor.close()
    db.close()

    belief_summary = ""
    if existing:
        belief_summary += "\nCurrent self-beliefs:\n"
        for row in existing:
            belief_summary += (
                f"- {row['beliefType']}: {row['beliefValue']} "
                f"(confidence {round(row['confidence'],2)}, "
                f"stability {round(row['stability'],2)})\n"
            )

    # ----------------------------------------
    # SYSTEM PROMPT
    # ----------------------------------------
    system = f"""
        You are simulating how THIS NPC forms beliefs about itself.

        The NPC may form or revise beliefs in the following domains ONLY:

        - age
        - gender
        - race
        - physical_appearance
        - identity
        - role
        - life_history
        - likes
        - dislikes
        - moral_alignment
        - personality_trait
        - goal
        - fear
        - worldview
        - current_environment
        - environment_social
        - environment_physical
        - current_state
        - physical_condition

        Do NOT invent other belief types.

        NPC PROFILE
        -----------
        Age: {npc['age']}
        Gender: {npc['gender']}
        Role: {npc['role']}
        Personality traits: {npc['personality_traits']}
        Emotional tendencies: {npc['emotional_tendencies']}
        Moral alignment: {npc['moral_alignment']}
        Current dominant emotion: {npc_emotion}

        {belief_summary}

        Belief Revision Rules:
        ----------------------
        - Only revise domains clearly supported by the NPC's own words.
        - Do not output beliefs for domains not referenced or implied.
        - High confidence (0.9+) requires explicit self-reference.
        - Stability should be HIGH (0.8+) when reinforcing core identity domains.
        - Stability should be LOW (0.2–0.5) for temporary states, doubts, or situational conditions.
        - If a core identity trait is being questioned, stability may decrease but should not collapse without explicit self-contradiction.
        - Do NOT hallucinate major backstory.
        - Do NOT act as an omniscient narrator.
        - Return ONLY valid JSON.
        - Age, gender, and race are immutable unless explicitly corrected by the NPC.
        - Do not weaken or contradict immutable traits without explicit self-correction.

        - environment_physical = physical setting (location, weather, room, city).
        - environment_social = people present, group dynamics, social hierarchy.
        - current_environment = general situational state if unclear.

        Core Identity Domains:
        ----------------------
        The following belief types are considered CORE identity traits:

        - age
        - gender
        - race
        - identity
        - role
        - moral_alignment
        - personality_trait
        - worldview

        These represent enduring aspects of self-concept.

        All other domains (current_state, environment_*, physical_condition, temporary fears, etc.)
        are considered situational or dynamic.

        """


    system += f"\nRecent interaction summary:\n{recent_context}\n"

    user = f"""
        NPC spoken text:
        \"\"\"{npc_output}\"\"\"

        Evaluate whether the NPC is expressing, reinforcing, weakening, or questioning beliefs about:

        - Age
        - Gender
        - Race
        - Physical appearance
        - Likes / Dislikes
        - Life history
        - Current environment (social or physical)
        - Identity or role
        - Personality traits
        - Goals or fears

        Return JSON in this exact format:

        {{
        "beliefs": [
            {{
            "beliefType": string,
            "beliefValue": string,
            "confidence": float,
            "stability": float
            }}
        ]
        }}

        Rules:
        - Only include domains clearly supported by the NPC's words.
        - Do NOT fabricate race or physical traits unless explicitly mentioned.
        - Do NOT guess immutable traits randomly.
        - Confidence between 0.0 and 1.0.
        - Stability between 0.0 and 1.0.
        - Below 0.3 confidence → omit.
        - No extra keys.
        - beliefValue must be concise and normalized (snake_case, no long sentences).
        """

    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    result = _safe_json_from_model(
        resp,
        fallback={"beliefs": []}
    )
    
    ALLOWED_SELF_TYPES = {
        "age",
        "gender",
        "race",
        "physical_appearance",
        "identity",
        "role",
        "life_history",
        "likes",
        "dislikes",
        "moral_alignment",
        "personality_trait",
        "goal",
        "fear",
        "worldview",
        "current_environment",
        "environment_social",
        "environment_physical",
        "current_state",
        "physical_condition"
    }

    # normalization
    beliefs = result.get("beliefs", [])
    cleaned = []

    for b in beliefs:
        if (
            isinstance(b, dict)
            and b.get("beliefType") in ALLOWED_SELF_TYPES
            and "beliefValue" in b
            and "confidence" in b
            and "stability" in b
        ):
            cleaned.append({
                "beliefType": str(b["beliefType"])[:50],
                "beliefValue": str(b["beliefValue"])[:255],
                "confidence": max(0.0, min(1.0, float(b["confidence"]))),
                "stability": max(0.0, min(1.0, float(b["stability"])))
            })

    print(f"\nBELIEFS ABOUT SELF: {cleaned}\n")

    return {"beliefs": cleaned}
#------------------------------------------------------------------
def merge_self_beliefs(idNPC, new_beliefs):
    db = connect()
    cursor = db.cursor(dictionary=True)

    for belief in new_beliefs:
        btype = belief["beliefType"]
        bvalue = belief["beliefValue"]
        new_conf = belief["confidence"]
        new_stability = belief["stability"]

        cursor.execute("""
            SELECT confidence, stability
            FROM npc_self_belief
            WHERE idNPC=%s AND beliefType=%s AND beliefValue=%s
        """, (idNPC, btype, bvalue))

        existing = cursor.fetchone()

        if existing:
            old_conf = existing["confidence"]
            stability = existing["stability"]

            updated_conf = old_conf + (new_conf - old_conf) * (1 - stability)

            cursor.execute("""
                UPDATE npc_self_belief
                SET confidence=%s, updatedAt=NOW()
                WHERE idNPC=%s AND beliefType=%s AND beliefValue=%s
            """, (updated_conf, idNPC, btype, bvalue))
        else:
            cursor.execute("""
                INSERT INTO npc_self_belief
                (idNPC, beliefType, beliefValue, confidence, stability)
                VALUES (%s,%s,%s,%s,%s)
            """, (idNPC, btype, bvalue, new_conf, new_stability))

    db.commit()
    cursor.close()
    db.close()
#------------------------------------------------------------------
# for logging
def record_classification_stats(idUser, idNPC, player_text, result):
    # -----------------------------------
    # RESEARCH LOG INSERT
    # -----------------------------------

    db = connect()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO player_input_classification_log (
            idUser,
            idNPC,
            playerText,
            sentiment,
            intensity,
            offensive,
            emotion,
            target,
            trust_delta,
            modelUsed,
            temperature
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        idUser,
        idNPC,
        player_text,
        result.get("sentiment"),
        float(result.get("intensity", 0.0)),
        int(result.get("offensive", False)),
        result.get("emotion"),
        result.get("target"),
        result.get("trust_delta", 0),
        "gpt-4.1",
        0.0
    ))

    db.commit()
    cursor.close()
    db.close()

#------------------------------------------------------------------
def build_classification_context(raw_mem: str, max_entries: int = 12):
    """
    Build a neutral, factual summary of recent NPC-player interaction
    for intent classification context.
    """

    if not raw_mem:
        return None

    lines = [l.strip() for l in raw_mem.splitlines() if l.strip()]
    recent = lines[-max_entries:]

    summary_lines = []
    last_emotion = None
    last_trust_delta = None

    for line in recent:

        # -------------------------------
        # PLAYER TEXT (precise match)
        # -------------------------------
        if "] [player responded to you]" in line:
            try:
                text = line.split("]", 2)[-1].strip()
                summary_lines.append(f"Player said: {text}")
            except Exception:
                continue

        # -------------------------------
        # NPC RESPONSE (compressed)
        # -------------------------------
        elif "] [You just responded to" in line:
            try:
                # Extract everything after the first single quote
                first_quote = line.find("'")
                last_quote = line.rfind("'")

                if first_quote != -1 and last_quote != -1 and last_quote > first_quote:
                    text = line[first_quote + 1:last_quote].strip()
                    summary_lines.append(f"You: {text}")
            except Exception:
                continue

        # -------------------------------
        # CLASSIFICATION JSON (robust)
        # -------------------------------
        elif "was classified as:" in line:
            try:
                # Find the JSON/dict starting point safely
                start = line.find("{")
                if start != -1:
                    json_part = line[start:]
                    data = ast.literal_eval(json_part)

                    last_emotion = data.get("emotion")
                    last_trust_delta = data.get("trust_delta")

            except Exception:
                continue

    # Attach extracted structured signals
    if last_emotion:
        summary_lines.append(f"Last detected player emotion: {last_emotion}")

    if last_trust_delta is not None:
        summary_lines.append(f"Last trust delta: {last_trust_delta}")

    if not summary_lines:
        return None

    return {
        "recent_summary": " ".join(summary_lines)
    }
#------------------------------------------------------------------
VALID_EMOTIONS = {
    "happy", "sad", "angry", "afraid",
    "calm", "excited", "disgusted"
}

def classify_npc_reaction(player_text, npc_output, idNPC, idUser, client):
    client = get_deepseek_client()

    db = connect()
    cursor = db.cursor(dictionary=True)

    # Persona
    cursor.execute("""
        SELECT p.personality_traits,
               p.emotional_tendencies,
               p.emotion_reactivity
        FROM npc_persona p
        WHERE p.idNPC = %s
    """, (idNPC,))
    persona = cursor.fetchone() or {}

    # Trust
    cursor.execute("""
        SELECT trust
        FROM playerNPCrelationship
        WHERE idNPC = %s AND idUser = %s
    """, (idNPC, idUser))
    rel = cursor.fetchone()

    trust = rel["trust"] if rel else 50

    cursor.close()
    db.close()

    system = f"""
    You determine the emotional state of THIS NPC
    after reacting to a player's statement.

    NPC personality traits: {persona.get('personality_traits')}
    Emotional tendencies: {persona.get('emotional_tendencies')}
    Trust toward player: {trust}

    The NPC's spoken response is the strongest evidence.

    Do NOT invent internal states not supported by tone.

    Return ONLY valid JSON:
    {{
        "emotion": one of [happy, sad, angry, afraid, calm, excited, disgusted],
        "intensity": number between 0.0 and 1.0
    }}
    """

    user = f"""
    Player said:
    \"\"\"{player_text}\"\"\"

    NPC responded:
    \"\"\"{npc_output}\"\"\"

    Determine the NPC's actual emotional state.
    """

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        )
        data = _safe_json_from_model(resp, fallback={"emotion": "calm", "intensity": 0.3})

    except Exception:
        # Absolute safe fallback
        return {"emotion": "calm", "intensity": 0.3}

    # -------------------------
    # HARD VALIDATION LAYER
    # -------------------------

    emotion = str(data.get("emotion", "")).lower().strip()
    intensity = data.get("intensity", 0.5)

    # Enforce valid emotion
    if emotion not in VALID_EMOTIONS:
        emotion = "calm"

    # Enforce numeric intensity
    try:
        intensity = float(intensity)
    except:
        intensity = 0.5

    # Clamp range
    intensity = max(0.0, min(1.0, intensity))

    return {
        "emotion": emotion,
        "intensity": intensity
    }
#------------------------------------------------------------------
def normalize_object_field(obj):
    if not isinstance(obj, dict):
        return None

    value = obj.get("value")
    conf  = obj.get("confidence")

    if not isinstance(value, str) or not value.strip():
        return None

    try:
        conf = float(conf)
    except:
        return None

    conf = max(0.0, min(1.0, conf))

    return {
        "value": value.strip()[:255],
        "confidence": conf
    }
#------------------------------------------------------------------
def normalize_list_field(lst):
    cleaned = []

    if not isinstance(lst, list):
        return cleaned

    for item in lst:
        if not isinstance(item, dict):
            continue

        value = item.get("value")
        conf  = item.get("confidence")

        if not isinstance(value, str) or not value.strip():
            continue

        try:
            conf = float(conf)
        except:
            continue

        conf = max(0.0, min(1.0, conf))

        cleaned.append({
            "value": value.strip()[:255],
            "confidence": conf
        })

    return cleaned



MEMORY_SCHEMA_INSTRUCTIONS = """
    You maintain the NPC's long-term memory document (kbText).
    This memory is SUBJECTIVE: it is filtered through the NPC's perceptions, biases, and current emotional state.
    However, you must preserve factual dialogue content verbatim inside quotes.

    CRITICAL OUTPUT RULES:
    - Output ONLY the updated memory document (no JSON, no commentary).
    - Preserve the exact document format shown below.
    - Never remove or alter any episode with Intensity >= 0.95 (hard preserve).
    - keep the scene header and scene peak intensity
    - Never break or rename keys/labels.
    Episode Numbering Rules:
    - Episodes must be numbered sequentially within each scene.
    - When adding new episodes, continue numbering from the last episode number in that scene.
    - "Responding to" must reference the correct episode number.

    DOCUMENT FORMAT (MUST MATCH):
    === SCENE: <scene_tag> ===
    Where: ...
    When: ...
    How we got here: ...
    NPC lens: ...

    Relevant beliefs in play (NPC about self):
    - <beliefType>: <beliefValue> (conf <x.xx>)
    Relevant beliefs in play (NPC about player):
    - <beliefType>: <beliefValue> (conf <x.xx>)

    EPISODES (in order)
    [1]
    Speaker: player|npc
    Said: "<verbatim>"
    Responding to: none|[N]
    Player felt (as I read it): <emotion> (<intensity>)         # only when Speaker=player
    How I felt hearing it: <emotion> (<intensity>)              # only when Speaker=player
    I felt speaking: <emotion> (<intensity>)                    # only when Speaker=npc
    I thought player felt: <emotion> (<intensity>)              # only when Speaker=npc
    Intensity: <0.00-1.00>
    Notes (my bias): <short subjective line>

    Scene peak intensity: <0.00-1.00>
    --- END SCENE ---

    If compression occurs within a scene, structure it like this:

    EPISODES (compressed)
    - <bullet summary line>
    - <bullet summary line>

    Compression Rules:
    - Retain the 5 most recent episodes fully detailed.
    - Preserve any episode with Intensity >= 0.95 OR containing high factual or identity relevance.
    - ALSO preserve any episode that establishes HIGH RELEVANCE.

    An episode is HIGH RELEVANCE if it:
    - Introduces a new factual anchor (name, date, place, role, number)
    - Defines a core belief or moral claim
    - Alters trust or relationship status
    - Establishes long-term goals or secrets
    - Represents a boundary rupture

    - When compressing, retain any concrete factual details explicitly in bullet form.
    Do not abstract these into vague descriptions.
    - Never generalize or remove names, dates, numbers, locations, job titles, or stated moral claims.
    - If a compressed episode contains factual anchors, include them explicitly in the bullet summary

    EPISODES (in order)
    [most recent episodes continue here]

    """

def clamp01(x: float) -> float:
    try:
        x = float(x)
    except Exception:
        return 0.5
    return max(0.0, min(1.0, x))

def format_beliefs_for_prompt(rows, label: str, min_conf=0.6, max_items=12):
    """
    rows: list of dicts like {beliefType, beliefValue, confidence}
    """
    if not rows:
        return f"{label}: (none)\n"
    # filter + sort
    filtered = [r for r in rows if float(r.get("confidence", 0)) >= min_conf]
    filtered.sort(key=lambda r: float(r.get("confidence", 0)), reverse=True)
    filtered = filtered[:max_items]
    out = f"{label}:\n"
    for r in filtered:
        out += f"- {r.get('beliefType')}: {r.get('beliefValue')} (conf {round(float(r.get('confidence',0)),2)})\n"
    return out

def update_structured_kbtext(
    *,
    client,
    idUser: int,
    idNPC: int,
    kbtext_current: str,
    player_text: str | None,
    npc_text: str | None,
    player_cls: dict | None,
    npc_reaction: dict | None,
    relevant_self_beliefs: list,
    relevant_player_beliefs: list,
    max_chars: int = 12000
) -> str:
    """
    LLM-determined scene structuring.
    Returns updated kbText.
    """
    client = get_deepseek_client()

    print(f"\nUPDATING KB\n")

    past_memory, current_scene, _ = get_most_recent_scene(kbtext_current or "")

    scene_for_llm = current_scene or "EMPTY"

    episode_count = scene_for_llm.count("\n[")
    should_compress_scene = episode_count >= 5


    compression_instruction = ""
    if should_compress_scene:

        print("\nCOMPRESSING SCENE\n")

        compression_instruction = """
        The current scene has grown long.
        Compress older low-intensity episodes within THIS SCENE.
        Keep the 5 most recent episodes fully detailed.
        Preserve any episode with Intensity >= 0.95.
        Replace older compressed episodes with a short bullet summary under:
        EPISODES (compressed)
    """

    # --------------------------------------------------
    # Fetch trust + relationship state
    # --------------------------------------------------
    db = connect()
    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT trust, wasEnemy
        FROM playerNPCrelationship
        WHERE idNPC = %s AND idUser = %s
    """, (idNPC, idUser))

    rel = cursor.fetchone()

    trust = rel["trust"] if rel else 50
    was_enemy = rel["wasEnemy"] if rel else 0

    relationship_type = phase_2_queries.determine_relationship_label(trust)

    if was_enemy:
        relationship_type = "former_enemy"

    # --------------------------------------------------
    # Emotional context
    # --------------------------------------------------
    perceived_player_emotion = (player_cls or {}).get("emotion", "calm")
    perceived_player_intensity = clamp01((player_cls or {}).get("intensity", 0.3))

    npc_emotion = (npc_reaction or {}).get("emotion", "calm")
    npc_intensity = clamp01((npc_reaction or {}).get("intensity", 0.3))

    episode_intensity = clamp01(max(perceived_player_intensity, npc_intensity))

    # --------------------------------------------------
    # Belief formatting
    # --------------------------------------------------
    self_belief_text = format_beliefs_for_prompt(
        relevant_self_beliefs,
        "SELF BELIEFS (candidate)",
        min_conf=0.6
    )

    player_belief_text = format_beliefs_for_prompt(
        relevant_player_beliefs,
        "PLAYER BELIEFS (candidate)",
        min_conf=0.6
    )

    # --------------------------------------------------
    # System Prompt
    # --------------------------------------------------
    system = f"""
    {MEMORY_SCHEMA_INSTRUCTIONS}

    SCENE CREATION POLICY (STRICT)

    A SCENE represents a single continuous activity in a single primary setting.

    You MUST create a NEW SCENE when ANY of the following occur:

    1. A meaningful physical location change

    2. A completed activity transitions into a new activity

    3. A clear time shift

    4. A tone reset or interaction shift

    5. A narrative beat that feels like a “chapter break.”

    DO NOT treat large setting transitions as continuation.
    If unsure, prefer creating a NEW SCENE rather than extending the old one.

    When creating a new scene:
    - Generate a new scene tag.
    - Reset episode numbering starting from [1].
    - Recalculate Scene peak intensity from episodes within that scene only.
    - Do NOT carry forward EPISODES (compressed) from previous scene.
    - Keep past scenes intact and append the new scene after them.

    Subjective Memory Rules:
    - Memory is filtered through the NPC's perception.
    - Dialogue must remain verbatim inside quotes.
    - Interpret emotions and motivations according to trust and personality.
    - Notes (my bias) should reflect emotion + beliefs.
    - Include only beliefs directly influencing this moment (max 6 each).

    FACT PRESERVATION RULES (STRICT)

    When compressing episodes, you MUST preserve specific factual anchors verbatim if they appear:

    - Dates (years, exact times, ages)
    - Place names (cities, states, schools, companies)
    - Dollar amounts or numbers
    - Names of people
    - Job titles or roles
    - Specific claims stated as facts
    - Any self-defining moral statements

    If an episode contains factual anchors, you may compress emotional interpretation,
    but you must retain those factual details explicitly inside the compressed summary.

    Never generalize or abstract away specific names, dates, numbers, or locations.
    Never replace them with vague summaries.


    RELATIONSHIP CONTEXT
    --------------------
    Relationship type: {relationship_type}
    Trust level: {trust} (0 = hostile, 50 = neutral, 100 = deeply trusting)

    High trust → generous interpretation.
    Low trust → guarded or suspicious interpretation.

    New episode intensity: {round(episode_intensity, 2)}

    If CURRENT SCENE is "EMPTY", create a new scene from scratch using the required format.

    {compression_instruction}
    """

    # --------------------------------------------------
    # User Prompt
    # --------------------------------------------------
    user = f"""
        CURRENT SCENE:
        \"\"\"{scene_for_llm}\"\"\"

        NEW TURN DATA:

        Player said:
        \"\"\"{player_text or ""}\"\"\"

        Player classifier:
        {json.dumps(player_cls or {}, ensure_ascii=False)}

        NPC responded:
        \"\"\"{npc_text or ""}\"\"\"

        NPC reaction:
        {json.dumps(npc_reaction or {}, ensure_ascii=False)}

        Candidate beliefs:
        {self_belief_text}
        {player_belief_text}

        Update the scene document.
        """

    # --------------------------------------------------
    # LLM Call
    # --------------------------------------------------
    resp = client.chat.completions.create(
        model="deepseek-reasoner",
        temperature=0.0,
        messages=[
            {"role": "system", "content": system.strip()},
            {"role": "user", "content": user.strip()},
        ],
    )

    updated = (resp.choices[0].message.content or "").strip()

    # --------------------------------------------------
    # Structural Guard
    # --------------------------------------------------
    if "=== SCENE:" not in updated or "--- END SCENE ---" not in updated:
        safe = kbtext_current or ""
        if player_text:
            safe += f"\n\n[{datetime.now()}] Player: {player_text}"
        if npc_text:
            safe += f"\n[{datetime.now()}] NPC: {npc_text}"
        return safe
    
    # --------------------------------------------------
    # Reassemble Full Memory (past + updated scene)
    # --------------------------------------------------

    if past_memory:
        final_memory = past_memory.rstrip() + "\n\n" + updated
    else:
        final_memory = updated

    # print(f"\nUPDATED MEM: {final_memory}\n")

    return final_memory

# --------------------------------------------------
def _safe_json_from_model(resp, fallback: dict):
    """
    Robust JSON extraction for DeepSeek-style outputs.
    - strips markdown fences
    - extracts first {...} block if model adds extra text
    """
    try:
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return fallback

        # Strip markdown fences ```json ... ```
        if raw.startswith("```"):
            parts = raw.split("```")
            if len(parts) >= 2:
                raw = parts[1].strip()

        # If the model added extra text, extract the first JSON object
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            raw = raw[first:last + 1]

        return json.loads(raw)

    except Exception:
        print("JSON parse failure. Raw output:", (resp.choices[0].message.content or "")[:500])
        return fallback


# --------------------------------------------------
import re

def get_most_recent_scene(kbtext: str):
    if not kbtext:
        return None, None, None

    pattern = r"(?ms)^=== SCENE:.*?^--- END SCENE ---\s*"
    scenes = list(re.finditer(pattern, kbtext))

    if not scenes:
        # No structured scene exists yet
        return None, None, kbtext.strip()

    last_match = scenes[-1]

    last_scene = last_match.group()
    scene_end_index = last_match.end()

    past_memory = kbtext[:last_match.start()]
    raw_buffer = kbtext[scene_end_index:]

    return past_memory.strip(), last_scene.strip(), raw_buffer.strip()