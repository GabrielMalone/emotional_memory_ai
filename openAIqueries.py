from openai import OpenAI
from flask import request, jsonify
import json
import ast
#------------------------------------------------------------------
import os
import mysql.connector
from datetime import datetime, timezone
#------------------------------------------------------------------
def connect()->object:
    return mysql.connector.connect(
        user=os.getenv('DB_USER'), 
        password=os.getenv('DB_PASSWORD'), 
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST', 'localhost') )
#------------------------------------------------------------------
def getResponseStream(prompt, current_scene, player_name, client):
    try:
        response = client.chat.completions.create(
            model="gpt-4.1",
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

    print(f"\nCLASSIFIER INPUT: {player_text}\n")

    # -----------------------------------
    # Build memory context
    # -----------------------------------
    mem_context = build_classification_context(raw_mem)

    # -----------------------------------
    # Fetch NPC persona + beliefs
    # -----------------------------------
    db = connect()
    cursor = db.cursor(dictionary=True)

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

    cursor.close()
    db.close()

    # Format beliefs
    belief_text = ""
    if beliefs:
        belief_text += "\nCurrent beliefs about the player:\n"
        for b in beliefs:
            belief_text += f"- {b['beliefType']}: {b['beliefValue']} (confidence {round(b['confidence'],2)})\n"

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

    Return ONLY valid JSON.
    Do not explain.
    Follow schema exactly.
    If no emotion clearly fits, use 'calm'.
    Do NOT invent new labels.
    """

    if mem_context and "recent_summary" in mem_context:
        system += f"\nRecent interaction summary:\n{mem_context['recent_summary']}\n"

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
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    result = json.loads(resp.choices[0].message.content)

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


    return result
#------------------------------------------------------------------
def extract_persona_clues(player_text: str, recent_context: dict, client, idNPC, idUser):

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

    if recent_context and "recent_summary" in recent_context:
        system += (
            "\nRecent interaction summary (may provide behavioral patterns):\n"
            f"{recent_context['recent_summary']}\n"
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
    - Do NOT guess randomly.
    - No extra keys.
    """

    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    # ------------------------------
    # Safe JSON parsing
    # ------------------------------
    try:
        result = json.loads(resp.choices[0].message.content)
    except Exception:
        return {
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

    return result

#------------------------------------------------------------------
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
            model="gpt-4.1",
            temperature=0.0,
            response_format={"type": "json_object"},  # 🔒 Force JSON mode
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        )

        raw = resp.choices[0].message.content
        data = json.loads(raw)

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