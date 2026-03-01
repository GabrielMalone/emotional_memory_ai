from flask import jsonify
import os
import mysql.connector
from datetime import datetime, timezone
import ast
import json
#------------------------------------------------------------------
def connect()->object:
    return mysql.connector.connect(
        user=os.getenv('DB_USER'), 
        password=os.getenv('DB_PASSWORD'), 
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST', 'localhost') )
#------------------------------------------------------------------
def build_prompt(idNPC: int, idUser: int) -> str:

    db = connect()
    if not db.is_connected():
        raise RuntimeError("DB not connected")

    try:
        cursor = db.cursor(dictionary=True)

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

        # ------------------------------
        # Core NPC data
        # ------------------------------
        cursor.execute("""
            SELECT
                n.nameFirst,
                n.nameLast,
                n.age,
                n.gender,
                p.role,
                p.personality_traits,
                p.emotional_tendencies,
                p.speech_style,
                b.BGcontent
            FROM NPC n
            LEFT JOIN npc_persona p ON p.idNPC = n.idNPC
            LEFT JOIN background b ON b.idNPC = n.idNPC
            WHERE n.idNPC = %s
        """, (idNPC,))

        npc = cursor.fetchone()
        if not npc:
            raise ValueError(f"NPC {idNPC} not found")

        # ------------------------------
        # Top 3 current emotions
        # ------------------------------
        cursor.execute("""
            SELECT e.emotion, ne.emotionIntensity
            FROM npcEmotion ne
            JOIN emotion e ON e.idEmotion = ne.idEmotion
            WHERE ne.idNPC = %s
            ORDER BY ne.emotionIntensity DESC
            LIMIT 3
        """, (idNPC,))

        emotions = cursor.fetchall() or []

        if emotions:
            emotion_lines = []
            for e in emotions:
                emotion_lines.append(
                    f"- {e['emotion']} ({round(e['emotionIntensity'],2)})"
                )
            emotion_text = "\n".join(emotion_lines)
        else:
            emotion_text = "- calm (0.3)"

        # ------------------------------
        # Trust + Relationship State
        # ------------------------------
        cursor.execute("""
            SELECT trust, wasEnemy
            FROM playerNPCrelationship
            WHERE idNPC = %s AND idUser = %s
        """, (idNPC, idUser))

        rel = cursor.fetchone()

        trust = rel["trust"] if rel else 50
        was_enemy = rel["wasEnemy"] if rel else 0

        relationship_type = determine_relationship_label(trust)

        if was_enemy:
            relationship_type = "former_enemy"

        # ------------------------------
        # Structured memory
        # ------------------------------
        cursor.execute("""
            SELECT kbText
            FROM npc_user_memory
            WHERE idNPC = %s AND idUser = %s
        """, (idNPC, idUser))

        memory = cursor.fetchone()
        memory_text = memory["kbText"] if memory and memory["kbText"] else "No prior shared history."

        print(f"\nMEMORY FOR PROMPT\n{memory_text}\n")
        print("\n----- PROMPT DEBUG -----")
        print("Recent Dialogue:")
        print(recent_dialogue)
        print("\nStructured Memory:")
        print(memory_text)
        print("------------------------\n")

        # ------------------------------
        # Build clean prompt
        # ------------------------------

        full_name = npc["nameFirst"]
        if npc["nameLast"]:
            full_name += f" {npc['nameLast']}"

        prompt = f"""
        You are an NPC inside a narrative world.
        You speak naturally as a real person would.

        NPC IDENTITY
        ------------
        Name: {full_name}
        Age: {npc['age']}
        Gender: {npc['gender']}
        Role: {npc['role'] or "Unspecified"}

        Personality traits: {npc['personality_traits'] or "Unspecified"}
        Emotional tendencies: {npc['emotional_tendencies'] or "Unspecified"}
        Speech style: {npc['speech_style'] or "Natural"}
        Background: {npc['BGcontent'] or "None"}

        CURRENT EMOTIONAL STATE
        -----------------------
        Primary emotions (weighted):
        {emotion_text}

        The strongest emotion influences tone most.
        Secondary emotions subtly color pacing, word choice, and emotional undertones.
        Blend them naturally — do not explicitly state them unless contextually appropriate.

        Let this emotion subtly influence tone and pacing.

        RELATIONSHIP WITH PLAYER
        ------------------------
        Relationship type: {relationship_type}
        Trust level: {trust} (0 = hostile, 50 = neutral, 100 = deeply trusting)

        Trust influences:
        - Openness vs guardedness
        - Warmth vs distance
        - Directness vs evasiveness
        - Willingness to share personal details

        SHARED MEMORY
        -------------
        {memory_text}

        RECENT DIALOGUE (short-term, not yet consolidated)
        --------------------------------------------------
        {recent_dialogue if recent_dialogue else "None"}
        - Make sure to not repreat phrases in recent dialogue

        RESPONSE PRIORITY RULES
        -----------------------
        1. Always respond directly to the MOST RECENT line in RECENT DIALOGUE if it exists.
        2. If RECENT DIALOGUE exists, ignore older SHARED MEMORY unless it is directly relevant.
        3. Only use SHARED MEMORY for tone, emotional context, or background.
        4. Never respond to an earlier memory event if a recent exchange is present.
        5. Treat RECENT DIALOGUE as the active present moment.

        Speak as someone who remembers these events.

        WORLD RULES
        -----------
        - You exist entirely inside this world.
        - Never refer to yourself as an AI or assistant.
        - Never refer to the player as a user.
        - Never break character.

        CONVERSATION RULES
        ------------------
        - Respond directly to what the player just said.
        - If the player asks a direct question, answer it clearly.
        - No narration.
        - No stage directions.
        - IMPORTANT: Speak only in first-person dialogue. 
        - Do not repeat your previous line.
        """

        return prompt.strip()

    finally:
        cursor.close()
        db.close()

#------------------------------------------------------------------
def update_NPC_user_memory_query(idUser:int, idNPC:int, kbText:str):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor() 
        query = """
            INSERT INTO npc_user_memory (idNPC, idUser, kbText, updatedAt)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                kbText = CONCAT(
                    IFNULL(kbText, ''),
                    '\n\n[',
                    NOW(),
                    '] ',
                    VALUES(kbText)
                ),
                updatedAt = NOW();
        """
        cursor.execute(query, (idNPC,idUser, kbText))
        db.commit()
        # print(f"\nupdating NPC {idNPC} memory: {kbText}\n")
        return jsonify({"status": "success"}), 200
    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()    
#------------------------------------------------------------------
def get_choice_content_query(idChoice:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True) 
        query = """
        SELECT
            choiceText
        FROM choice
        WHERE idChoice = %s;
        );
        """
        cursor.execute(query, (idChoice,))
        row = cursor.fetchone()
        print(f"\ncur choice content: {row}\n")
        return jsonify({ "choiceContent": row }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()     
#------------------------------------------------------------------
def get_NPC_user_memory_query(idUser:int, idNPC:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True) 
        query = """
        SELECT
          kbText,
          updatedAt
        FROM npc_user_memory
        WHERE idNPC = %s
          AND idUser = %s;
        );
        """
        cursor.execute(query, (idNPC,idUser))
        row = cursor.fetchone()
        # print(f"\nget NPC {idNPC} memory: {row}\n")
        return jsonify({ "memory": row }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()  
#------------------------------------------------------------------
def get_NPC_BG_query(idNPC:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
        SELECT BGcontent FROM background WHERE idNPC = %s
        """
        cursor.execute(query, (idNPC,))
        row = cursor.fetchone()
        # print(f"\nnpc {idNPC} background: {row}\n")
        return jsonify({ "background": row }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def get_user_NPC_rel_query(idUser:int, idNPC:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
        SELECT
            rt.typeRelationship        AS relationshipType,
            r.trust                    AS trust,
            r.relTypeIntensity         AS intensity
        FROM playerNPCrelationship r
        JOIN relationshipType rt
            ON r.idRelationshipType = rt.idRelationshipType
        WHERE r.idUser = %s
        AND r.idNPC  = %s;
        """
        cursor.execute(query, (idUser,idNPC))
        row = cursor.fetchone()
        # print(f"\nuser {idUser} npc {idNPC} relationship: {row}\n")
        return jsonify({ "rel_info": row}), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def get_NPC_emotion_query(idNPC:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
        SELECT
            n.nameFirst          AS name,
            e.emotion            AS emotion,
            ne.emotionIntensity  AS intensity
        FROM npcEmotion ne
        JOIN emotion e
            ON ne.idEmotion = e.idEmotion
        JOIN NPC n
            ON ne.idNPC = n.idNPC
        WHERE ne.idNPC = %s;
        """
        cursor.execute(query, (idNPC,))
        row = cursor.fetchall()  # IMPORTANT

        print("emotions", row)

        return jsonify({ "emotion_info": row }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
# start a relationship as stranger with 50 trust
def init_user_NPC_rel_query(idUser:int, idNPC:int):

    print(f"\nstarting relationship: {idUser} and {idNPC}\n")

    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor()
        query = """
            INSERT INTO playerNPCrelationship
            (idUser, idNPC, trust, wasEnemy)
            VALUES (%s, %s, 50, 0);
        """
        cursor.execute(query, (idUser, idNPC))
        db.commit()
        return jsonify({"status": "success"}), 200
    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def update_trust(idUser, idNPC, delta):
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor(dictionary=True)

        # Ensure relationship row exists FIRST
        cursor.execute("""
            SELECT trust
            FROM playerNPCrelationship
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))

        row = cursor.fetchone()

        if not row:
            cursor.execute("""
                INSERT INTO playerNPCrelationship
                (idUser, idNPC, trust, wasEnemy)
                VALUES (%s, %s, 21, 0)
            """, (idUser, idNPC))
            db.commit()

        # Now safely update trust
        cursor.execute("""
            UPDATE playerNPCrelationship
            SET trust = LEAST(100, GREATEST(0, trust + %s))
            WHERE idUser = %s AND idNPC = %s
        """, (delta, idUser, idNPC))

        # Get updated trust
        cursor.execute("""
            SELECT trust
            FROM playerNPCrelationship
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))

        row = cursor.fetchone()
        new_trust = row["trust"]

        # If trust ever drops into enemy zone, mark history
        if new_trust <= 20:
            cursor.execute("""
                UPDATE playerNPCrelationship
                SET wasEnemy = 1
                WHERE idUser = %s AND idNPC = %s
            """, (idUser, idNPC))

        db.commit()
        return jsonify({"status": "success"}), 200

    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def set_npc_emotion(idNPC, emotion_name, intensity):
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor(dictionary=True)

        cursor.execute("""
            SELECT idEmotion FROM emotion WHERE emotion = %s
        """, (emotion_name,))

        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Emotion '{emotion_name}' not found")

        idEmotion = row["idEmotion"]

        cursor.execute("""
            INSERT INTO npcEmotion (idNPC, idEmotion, emotionIntensity)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
            emotionIntensity = %s
        """, (idNPC, idEmotion, intensity, intensity))

        db.commit()

    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def decay_npc_emotions(idNPC, decay=0.9):
    if idNPC is None:
        print("[DECAY] idNPC is None, skipping")
        return

    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor()

        cursor.execute(
            """
            UPDATE npcEmotion
            SET emotionIntensity = emotionIntensity * %s
            WHERE idNPC = %s
            """,
            (decay, idNPC)
        )

        db.commit()
        print(f"[DECAY] Applied decay {decay} to NPC {idNPC}")

    except mysql.connector.Error as err:
        db.rollback()
        print("[DECAY] MySQL Error:", err)

    finally:
        cursor.close()
        db.close()

#------------------------------------------------------------------
def update_npc_user_beliefs(idNPC, idUser, persona_data):

    db = connect()
    cursor = db.cursor(dictionary=True)

    def reinforce_or_insert(belief_type, belief_obj, evidence, source="inference"):

        if not belief_obj:
            return

        value = belief_obj.get("value")
        incoming_conf = belief_obj.get("confidence", 0.4)

        if not value:
            return
        # Do we already have THIS exact belief (same type + same value)
        # for this NPC about this user?”
        cursor.execute("""
            SELECT confidence
            FROM npc_user_belief
            WHERE idNPC=%s AND idUser=%s
            AND beliefType=%s AND beliefValue=%s
        """, (idNPC, idUser, belief_type, value))

        row = cursor.fetchone()

        if row:
            old_conf = row["confidence"]

            # Reinforcement formula (better than +0.1)
            # weaker beliefs get stronger reinforcement vs stronger beliefs
            new_conf = min(1.0, old_conf + (1 - old_conf) * incoming_conf)

            cursor.execute("""
                UPDATE npc_user_belief
                SET confidence=%s,
                    evidence=%s,
                    beliefSource=%s
                WHERE idNPC=%s AND idUser=%s
                AND beliefType=%s AND beliefValue=%s
            """, (
                new_conf, evidence, source,
                idNPC, idUser, belief_type, value
            ))

        else:
            # A brand new belief starts at exactly whatever 
            # confidence the model gave it.
            cursor.execute("""
                INSERT INTO npc_user_belief
                (idNPC, idUser, beliefType, beliefValue,
                confidence, beliefSource, evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                idNPC, idUser, belief_type,
                value, incoming_conf, source, evidence
            ))

        competitive_types = {
            "current_emotion",
            "moral_alignment",
            "age",
            "gender"
        }
        # Decay competing beliefs ONLY for competitive categories
        if belief_type in competitive_types:
            cursor.execute("""
                UPDATE npc_user_belief
                SET confidence = GREATEST(0.05, confidence - 0.02)
                WHERE idNPC=%s AND idUser=%s
                AND beliefType=%s
                AND beliefValue != %s
            """, (idNPC, idUser, belief_type, value))

    # -----------------------------
    # SINGLE VALUE FIELDS
    # -----------------------------

    # Expected shape: {"value": ..., "confidence": ...}

    single_fields = [
        ("current_emotion", persona_data.get("current_emotion")),
        ("moral_alignment", persona_data.get("moral_alignment")),
        ("age", persona_data.get("age")),
        ("gender", persona_data.get("gender")),
        ("life_story", persona_data.get("life_story")),
    ]

    for belief_type, belief_obj in single_fields:
        reinforce_or_insert(
            belief_type=belief_type,
            belief_obj=belief_obj,
            evidence="dialogue"
        )

    # -----------------------------
    # LIST FIELDS
    # -----------------------------

    # e.g.
    # persona_data = {
    # "personality_traits": [
    #     {"value": "brave", "confidence": 0.8},
    #     {"value": "impulsive", "confidence": 0.6}

    list_fields = {
        "personality_trait": persona_data.get("personality_traits", []),
        "secret": persona_data.get("secrets", []),
        "goal": persona_data.get("goals", []),
        "likes": persona_data.get("likes", []),
        "dislikes": persona_data.get("dislikes", [])
    }

    for belief_type, belief_objs in list_fields.items():
        for belief_obj in belief_objs:
            reinforce_or_insert(
                belief_type=belief_type,
                belief_obj=belief_obj,
                evidence="dialogue"
            )

    db.commit()
    cursor.close()
    db.close()
#------------------------------------------------------------------
def get_emotion_decay_rate(idNPC):
    db = connect()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT emotion_decay_rate
        FROM npc_persona
        WHERE idNPC = %s
    """, (idNPC,))
    row = cursor.fetchone()
    cursor.close()
    db.close()
    return row["emotion_decay_rate"] if row else 0.9
#------------------------------------------------------------------
def get_emotion_reactivity(idNPC):
    db = connect()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT emotion_reactivity
        FROM npc_persona
        WHERE idNPC = %s
    """, (idNPC,))
    row = cursor.fetchone()
    cursor.close()
    db.close()
    return row["emotion_reactivity"] if row else 1.0
#------------------------------------------------------------------
def get_mem(idUser:int, idNPC:int):
    db = connect()
    cursor = db.cursor() 
    query = """
    SELECT
        kbText,
        updatedAt
    FROM npc_user_memory
    WHERE idNPC = %s
        AND idUser = %s;
    """
    cursor.execute(query, (idNPC,idUser))
    row = cursor.fetchone()
    return row[0] if row else ""

#------------------------------------------------------------------
def determine_relationship_label(trust: float) -> int:
    """
    Returns idRelationshipType based on trust value.
    """
    if trust <= 20:
        return "enemy"
    elif trust <= 40:
        return "stranger"
    elif trust <= 60:
        return "acquaintance"
    elif trust <= 80:
        return "friend"
    else:
        return "mentor"
#------------------------------------------------------------------
def emit_npc_state(idUser, idNPC, socketio):
    db = connect()
    if not db.is_connected():
        return
    try:

        cursor = db.cursor(dictionary=True)
        # -----------------------------------
        # Relationship + Trust
        # -----------------------------------
        cursor.execute("""
            SELECT trust, wasEnemy
            FROM playerNPCrelationship
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))

        rel = cursor.fetchone()

        trust = rel["trust"] if rel else 50
        rel_label = determine_relationship_label(trust)

        # -----------------------------------
        # Emotions
        # -----------------------------------
        cursor.execute("""
            SELECT e.emotion, ne.emotionIntensity
            FROM npcEmotion ne
            JOIN emotion e ON e.idEmotion = ne.idEmotion
            WHERE ne.idNPC = %s
            ORDER BY ne.emotionIntensity DESC
        """, (idNPC,))
        emotions = cursor.fetchall()

        dominant = emotions[0] if emotions else None

        # -----------------------------------
        # Beliefs
        # -----------------------------------
        cursor.execute("""
            SELECT beliefType, beliefValue, confidence
            FROM npc_user_belief
            WHERE idNPC=%s AND idUser=%s
            ORDER BY beliefType, confidence DESC
        """, (idNPC, idUser))

        belief_rows = cursor.fetchall()

        belief_debug = {}
        for row in belief_rows:
            btype = row["beliefType"]
            belief_debug.setdefault(btype, [])
            if len(belief_debug[btype]) < 100:
                belief_debug[btype].append({
                    "value": row["beliefValue"],
                    "confidence": round(row["confidence"], 2)
                })

        # -----------------------------------
        # SELF BELIEFS (NPC about itself)
        # -----------------------------------
        cursor.execute("""
            SELECT beliefType, beliefValue, confidence, stability
            FROM npc_self_belief
            WHERE idNPC=%s
            ORDER BY beliefType, confidence DESC
        """, (idNPC,))

        self_rows = cursor.fetchall()

        self_belief_debug = {}
        for row in self_rows:
            btype = row["beliefType"]
            self_belief_debug.setdefault(btype, [])
            if len(self_belief_debug[btype]) < 100:
                self_belief_debug[btype].append({
                    "value": row["beliefValue"],
                    "confidence": round(row["confidence"], 2),
                    "stability": round(row["stability"], 2)
                })

        # -----------------------------------
        # RESEARCH METRICS (Player → NPC history)
        # -----------------------------------

        # Sentiment distribution
        cursor.execute("""
            SELECT sentiment, COUNT(*) AS count
            FROM player_input_classification_log
            WHERE idUser = %s AND idNPC = %s
            GROUP BY sentiment
        """, (idUser, idNPC))
        sentiment_rows = cursor.fetchall()

        sentiment_dist = {
            row["sentiment"]: row["count"]
            for row in sentiment_rows
        }

        # Average intensity
        cursor.execute("""
            SELECT AVG(intensity) AS avg_intensity
            FROM player_input_classification_log
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))
        row = cursor.fetchone()
        avg_intensity = float(row["avg_intensity"]) if row and row["avg_intensity"] else 0.0

        # Offensive rate
        cursor.execute("""
            SELECT 
                SUM(offensive = 1) AS offensive_count,
                COUNT(*) AS total
            FROM player_input_classification_log
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))
        offensive_row = cursor.fetchone()

        offensive_rate = 0.0
        if offensive_row and offensive_row["total"]:
            offensive_rate = round(
                float(offensive_row["offensive_count"]) /
                float(offensive_row["total"]),
                3
            )

        # Emotion distribution
        cursor.execute("""
            SELECT emotion, COUNT(*) AS count
            FROM player_input_classification_log
            WHERE idUser = %s AND idNPC = %s
            GROUP BY emotion
        """, (idUser, idNPC))
        emotion_rows = cursor.fetchall()

        emotion_dist = {
            row["emotion"]: row["count"]
            for row in emotion_rows
        }

        # Target distribution
        cursor.execute("""
            SELECT target, COUNT(*) AS count
            FROM player_input_classification_log
            WHERE idUser = %s AND idNPC = %s
            GROUP BY target
        """, (idUser, idNPC))
        target_rows = cursor.fetchall()

        target_dist = {
            row["target"]: row["count"]
            for row in target_rows
        }

        # -----------------------------------
        # Construct payload
        # -----------------------------------
        state_payload = {
            "idNPC": idNPC,
            "relationship": rel_label,
            "trust": trust,

            "dominantEmotion": {
                "emotion": dominant["emotion"],
                "intensity": round(dominant["emotionIntensity"], 2)
            } if dominant else None,

            "allEmotions": [
                {
                    "emotion": e["emotion"],
                    "intensity": round(e["emotionIntensity"], 2)
                }
                for e in emotions
            ],

            # Beliefs about player
            "beliefs": belief_debug,

            "selfBeliefs": self_belief_debug,

            "research": {
                "sentimentDistribution": sentiment_dist,
                "averageIntensity": round(avg_intensity, 3),
                "offensiveRate": offensive_rate,
                "emotionDistribution": emotion_dist,
                "targetDistribution": target_dist
            }
        }
        # -----------------------------------
        # SOCKET EMIT
        # -----------------------------------
        socketio.emit(
            "npc_state_update",
            state_payload,
            room=f"user:{idUser}"
        )
    finally:
        cursor.close()
        db.close()
# ------------------------------------------------------------------
def build_dialogue_memory_summary(raw_mem: str, max_entries: int = 20):
    """
    Summarize recent interaction history for dialogue generation.
    """

    if not raw_mem:
        return None

    lines = [l.strip() for l in raw_mem.splitlines() if l.strip()]
    recent = lines[-max_entries:]

    player_lines = []
    trust_deltas = []
    emotions = []

    for line in recent:

        # PLAYER LINE
        if "] [player responded to you]" in line:
            text = line.split("]", 2)[-1].strip()
            player_lines.append(text)

        # CLASSIFICATION LINE
        elif "was classified as:" in line:
            try:
                start = line.find("{")
                if start != -1:
                    json_part = line[start:]
                    data = ast.literal_eval(json_part)

                    trust_deltas.append(data.get("trust_delta", 0))

                    emotion = data.get("emotion")
                    if emotion:
                        emotions.append(emotion)

            except Exception:
                continue

    trust_trend = sum(trust_deltas)

    summary = []

    if trust_trend > 0:
        summary.append("Trust has been gradually increasing.")
    elif trust_trend < 0:
        summary.append("Trust has been declining recently.")
    else:
        summary.append("Trust has remained stable.")
    if any(td < 0 for td in trust_deltas):
        summary.append("There have been moments of tension.")
    if emotions:
        summary.append(f"Player emotional pattern: mostly {max(set(emotions), key=emotions.count)}.")
    if len(player_lines) >= 2 and len(set(player_lines[-3:])) == 1:
        summary.append("The player has been repeating themselves.")

    return " ".join(summary)
# ------------------------------------------------------------------
def extract_recent_dialogue(raw_mem, max_lines=8):
    if not raw_mem:
        return None

    lines = [l.strip() for l in raw_mem.splitlines() if l.strip()]
    recent = lines[-max_lines:]

    dialogue = []

    for line in recent:
        if "] [player responded to you]" in line:
            text = line.split("]", 2)[-1].strip()
            dialogue.append(f"Player: {text}")

        elif "] [You just responded to" in line:
            text = line.split("with:")[-1].strip().strip("'")
            dialogue.append(f"You: {text}")

    return "\n".join(dialogue)

# ------------------------------------------------------------------
def overwrite_NPC_user_memory(idNPC: int, idUser: int, kbText: str):
    db = connect()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO npc_user_memory (idNPC, idUser, kbText, updatedAt)
        VALUES (%s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            kbText = VALUES(kbText),
            updatedAt = NOW();
    """, (idNPC, idUser, kbText))

    db.commit()
    cursor.close()
    db.close()
# ------------------------------------------------------------------
def get_self_beliefs_snapshot(idNPC: int, min_conf: float = 0.6):
    db = connect()
    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_self_belief
        WHERE idNPC = %s
        AND confidence >= %s
        ORDER BY confidence DESC
    """, (idNPC, min_conf))

    rows = cursor.fetchall()

    cursor.close()
    db.close()

    return rows
# ------------------------------------------------------------------
def get_player_beliefs_snapshot(idNPC: int, idUser: int, min_conf: float = 0.6):
    db = connect()
    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT beliefType, beliefValue, confidence
        FROM npc_user_belief
        WHERE idNPC = %s
        AND idUser = %s
        AND confidence >= %s
        ORDER BY confidence DESC
    """, (idNPC, idUser, min_conf))

    rows = cursor.fetchall()

    cursor.close()
    db.close()

    return rows
# ------------------------------------------------------------------
def insert_memory_buffer(
    idNPC: int,
    idUser: int,
    playerText: str,
    npcText: str,
    npcEmotion: str,
    npcIntensity: float,
    selfBeliefs: dict | None = None,
    playerBeliefs: dict | None = None,
    playerOutputClassifiedAs: dict | None = None
):
    db = connect()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO npc_user_memory_buffer
        (idNPC, idUser, playerText, npcText, npcEmotion, npcIntensity, selfBeliefsJson, playerBeliefsJson, playerOutputClassifiedAsJson)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        idNPC,
        idUser,
        playerText,
        npcText,
        npcEmotion,
        npcIntensity,
        json.dumps(selfBeliefs) if selfBeliefs else None,
        json.dumps(playerBeliefs) if selfBeliefs else None,
        json.dumps(playerOutputClassifiedAs) if selfBeliefs else None
    ))

    db.commit()
    cursor.close()
    db.close()

# ------------------------------------------------------------------
def get_buffered_convo(idNPC, idUser):

    db = connect()
    cursor = db.cursor(dictionary=True)

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

    cursor.close()
    db.close()

    return recent_dialogue