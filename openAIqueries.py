from openai import OpenAI
from flask import request, jsonify
import json
#------------------------------------------------------------------
import os
import mysql.connector
from datetime import datetime, timezone
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
                    "role": "assistant",
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

        # i wonde rif here we classify again --- the reponse the model comes up with will have its own emotional context sometimes other 
        # than the emotion we give it

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
def classify_player_input(player_text: str, mem: dict, client):

    print(f"\nCLASSIFIER: {player_text} {mem}\n")

    system = (
        "You classify player dialogue directed at an NPC.\n"
        "Return ONLY valid JSON.\n"
        "Do not explain.\n"
        "You MUST follow the schema exactly.\n"
        "If no emotion clearly fits, use 'calm'.\n"
        "Do NOT invent new labels.\n"
    )

    if mem and "recent_summary" in mem:
        system += (
            "\nRecent interaction summary:\n"
            f"{mem['recent_summary']}\n"
            "Use this to help give context to the player's latest response.\n"
        )

    user = f"""
    Player text:
    \"\"\"{player_text}\"\"\"

    IMPORTANT:
    Determine whether the emotional tone is directed at:
    - the NPC
    - the player themself
    - the environment/situation
    - or no one in particular

    Classify with EXACTLY these fields and values:

    - sentiment: one of [positive, neutral, negative, hostile, affectionate]
    - intensity: number from 0.0 to 1.0
    - offensive: true or false
    - emotion: one of [happy, sad, angry, afraid, calm, excited, disgusted]
    - target: one of [npc, self, environment, none]

    Return JSON ONLY. No extra keys.
    """

    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )

    ALLOWED_EMOTIONS = {
        "happy", "sad", "angry", "afraid", "calm", "excited", "disgusted"
    }

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
        trust_delta = int(1 + intensity * 2)  # vulnerability scales up to +3

    elif sentiment == "affectionate":
        trust_delta = int(1 + intensity * 3)  # warmth scales up to +4

    elif sentiment == "positive" and target == "npc":
        trust_delta = int(1 + intensity * 2)

    result["trust_delta"] = trust_delta

    # ----------------------------
    # Emotion validation
    # ----------------------------
    if result.get("emotion") not in ALLOWED_EMOTIONS:
        print(f"[WARN] Invalid emotion '{result.get('emotion')}', defaulting to 'calm'")
        result["emotion"] = "calm"

    return result
#------------------------------------------------------------------
def extract_persona_clues(player_text: str, recent_context: dict, client, idNPC, idUser):

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

    cursor.close()
    db.close()

    system = f"""
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
        "current_emotion": string or null,
        "moral_alignment": string or null,
        "age": string or null,
        "gender": string or null,
        "life_story": string or null,
        "personality_traits": list of strings,
        "secrets": list of strings,
        "goals": list of strings
    }}

    Inference Guidelines:

    - All fields may be inferred from tone, behavior, word choice, patterns, or contradictions.
    - Explicit claims should not automatically override behavioral evidence.
    - If a player claims one emotion but language strongly suggests another,
        choose the more strongly supported belief.
    - personality_traits should reflect enduring patterns.
    - goals may be inferred from expressed desires or recurring motivations.
    - secrets may be inferred if the player implies concealment or avoidance.
    - life_story may be inferred if background context is strongly suggested.
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
            "goals": []
        }

    # ------------------------------
    # Safety normalization
    # ------------------------------

    expected_lists = ["personality_traits", "secrets", "goals"]
    for key in expected_lists:
        if not isinstance(result.get(key), list):
            result[key] = []

    expected_nullable = [
        "current_emotion",
        "moral_alignment",
        "age",
        "gender",
        "life_story"
    ]
    for key in expected_nullable:
        if not isinstance(result.get(key), str) or not result.get(key):
            result[key] = None

    return result
#------------------------------------------------------------------
def build_classification_context(raw_mem: str, max_entries: int = 6):
    """
    Build a neutral, factual summary of recent NPC_player interaction
    for intent classification context.
    """
    if not raw_mem:
        return None

    # split + clean
    # create a list of dialogue
    lines = [l.strip() for l in raw_mem.splitlines() if l.strip()]
    recent = lines[-max_entries:]

    summary_lines = []

    for line in recent:
        # player messages: preserve exact wording
        if "[player responded to you]" in line:
            try:
                text = line.split("]", 2)[-1].strip()
                summary_lines.append(f"Player said: {text}")
            except Exception:
                continue

        # NPC responses: compress to avoid overpowering context
        elif "[You just responded to" in line:
            summary_lines.append("NPC responded to the player.")

    if not summary_lines:
        return None

    return {
        "recent_summary": " ".join(summary_lines)
    }
#------------------------------------------------------------------
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
    persona = cursor.fetchone()

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

    NPC personality traits: {persona['personality_traits']}
    Emotional tendencies: {persona['emotional_tendencies']}
    Trust toward player: {trust}

    The NPC's spoken response is the strongest evidence.

    Do NOT invent internal states not supported by tone.

    Return ONLY valid JSON:
    {{
        "emotion": one of [happy, sad, angry, afraid, calm, excited, disgusted],
        "intensity": number 0.0–1.0
    }}
    """

    user = f"""
    Player said:
    \"\"\"{player_text}\"\"\"

    NPC responded:
    \"\"\"{npc_output}\"\"\"

    Determine the NPC's actual emotional state.
    """

    resp = client.chat.completions.create(
        model="gpt-4.1",
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    )

    return json.loads(resp.choices[0].message.content)