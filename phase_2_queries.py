from flask import request, jsonify
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
def build_prompt(idNPC: int, idUser: int) -> str:
    prompt = ""
    db = connect()
    if not db.is_connected():
        raise RuntimeError("DB not connected")
    try:
        cursor = db.cursor(dictionary=True) 
        # --------------------------------------------------
        # Core NPC + persona + background
        # --------------------------------------------------
        cursor.execute("""
            SELECT
            n.idNPC,
            n.nameFirst,
            n.nameLast,
            n.age,
            n.gender,
            p.role,
            p.personality_traits,
            p.emotional_tendencies,
            p.speech_style,
            p.moral_alignment,
            b.BGcontent
            FROM NPC n
            LEFT JOIN npc_persona p ON p.idNPC = n.idNPC
            LEFT JOIN background b ON b.idNPC = n.idNPC
            WHERE n.idNPC = %s;
        """, (idNPC,))
        npc = cursor.fetchone()

        if not npc:
            raise ValueError(f"NPC {idNPC} not found")

        # --------------------------------------------------
        # Dominant emotion
        # --------------------------------------------------
        cursor.execute("""
            SELECT e.emotion, ne.emotionIntensity
            FROM npcEmotion ne
            JOIN emotion e ON e.idEmotion = ne.idEmotion
            WHERE ne.idNPC = %s
            ORDER BY ne.emotionIntensity DESC
            LIMIT 2;
        """, (idNPC,))
        
        emotions = cursor.fetchall()
        dominant = emotions[0] if emotions else None
        secondary = emotions[1] if len(emotions) > 1 else None

        # --------------------------------------------------
        # Player relationship
        # --------------------------------------------------
        cursor.execute("""
            SELECT trust, wasEnemy
            FROM playerNPCrelationship
            WHERE idNPC = %s AND idUser = %s;
        """, (idNPC, idUser))
        relationship = cursor.fetchone()

        if relationship:
            trust = relationship["trust"]
            rel_label = determine_relationship_label(trust)

            prompt += f"""
            RELATIONSHIP WITH PLAYER
            ------------------------
            Relationship type: {rel_label}
            Trust: {trust}
            """

            if relationship["wasEnemy"]:
                prompt += """
                HISTORY NOTE:
                You once considered this player an enemy.
                That history still influences you subtly.
                """

        if not relationship:
            print(f'\nCREATING RELATIONSHIP between npc: {idNPC} and user: {idUser}\n')
            # default = stranger
            cursor.execute("""
                INSERT INTO playerNPCrelationship
                    (idUser, idNPC, idRelationshipType, relTypeIntensity, trust)
                VALUES (%s, %s,
                    (SELECT idRelationshipType
                    FROM relationshipType
                    WHERE typeRelationship = 'stranger'),
                    0,
                    50
                );
            """, (idUser, idNPC))

            db.commit()

            # re-fetch so prompt logic stays unchanged
            cursor.execute("""
                SELECT rt.typeRelationship, r.trust, r.relTypeIntensity
                FROM playerNPCrelationship r
                JOIN relationshipType rt
                ON rt.idRelationshipType = r.idRelationshipType
                WHERE r.idNPC = %s AND r.idUser = %s;
            """, (idNPC, idUser))

            relationship = cursor.fetchone()

        # --------------------------------------------------
        # beliefs about player
        # --------------------------------------------------
        cursor.execute("""
            SELECT beliefType, beliefValue, confidence
            FROM npc_user_belief
            WHERE idNPC = %s AND idUser = %s
            AND confidence >= 0.6
            ORDER BY beliefType, confidence DESC
        """, (idNPC, idUser))

        beliefs = cursor.fetchall()            
        belief_summary = {}
        # get top 3 beliefs for each belief 
        for b in beliefs:
            belief_summary.setdefault(b["beliefType"], [])
            if len(belief_summary[b["beliefType"]]) < 3:
                belief_summary[b["beliefType"]].append(b["beliefValue"])

        if belief_summary:
            prompt += "\n\nNPC'S CURRENT BELIEFS ABOUT THE PLAYER\n"
            prompt += "--------------------------------------\n"

            for btype, values in belief_summary.items():
                prompt += f"{btype.replace('_',' ').title()}:\n"
                for v in values:
                    prompt += f"- {v}\n"

        prompt += """

        BELIEF INTERPRETATION RULE
        --------------------------
        The above beliefs are your current working model of the player.

        You do not treat them as objective truth —
        but they strongly influence:

        - Your tone
        - Your level of warmth or suspicion
        - Your willingness to share information
        - Your emotional reactions
        - Your assumptions about the player's intentions

        If you believe the player is dangerous, selfish, dishonest, or hostile,
        your speech should reflect guardedness, caution, tension, or distrust.

        If you believe the player is kind, helpful, or loyal,
        your speech should reflect warmth, openness, or cooperation.

        You respond to the player as you currently perceive them.
        """

        all_belief_types = {
        "current_emotion",
        "moral_alignment",
        "age",
        "gender",
        "personality_trait",
        "secret",
        "goal"
        }

        known_types = set(belief_summary.keys())
        missing_types = list(all_belief_types - known_types)

        print(f"\nTYPES MISSING OR WITH CONFIDENCE < 0.6:\n", missing_types)

        if missing_types:
            prompt += f"""
            CURIOSITY PRIORITY
            ------------------
            Understanding the player is one of your ongoing goals.

            If there are major gaps in your understanding of the player,
            and the current emotional situation allows it,
            you may choose to make your conversational beat a question.

            Curiosity is not secondary to reaction.
            It is a legitimate driver of speech.

            When:
            - trust ≥ 40
            - no immediate threat is present
            - emotion intensity is below extreme levels

            When asking a curiosity-driven question,
            prefer questions that clarify missing belief categories
            while also being relevant to the current conversation.
            {", ".join(missing_types)}

            You are allowed to pivot the conversation slightly
            if doing so feels emotionally natural.

            """

        # --------------------------------------------------
        # Shared memory
        # --------------------------------------------------
        cursor.execute("""
            SELECT kbText, updatedAt
            FROM npc_user_memory
            WHERE idNPC = %s AND idUser = %s;
        """, (idNPC, idUser))
        memory = cursor.fetchone()

        # print(f"\nNPC MEMORY:\n", memory)

        interaction_context = None

        if memory and memory.get("updatedAt"):
            now = datetime.now(timezone.utc)
            last = memory["updatedAt"]
            # normalize DB datetime (MySQL DATETIME is naive)
            last = last.replace(tzinfo=timezone.utc)
            delta = (now - last).total_seconds()

            if delta < 90:
                interaction_context = "continuous"
            elif delta < 900:
                interaction_context = "recent"
            elif delta < 86400:
                interaction_context = "same_day"
            else:
                interaction_context = "long_gap"
        # --------------------------------------------------
        # Prompt assembly
        # --------------------------------------------------
        full_name = npc["nameFirst"]
        if npc["nameLast"]:
            full_name += f" {npc['nameLast']}"

        prompt += f"""
        You are an NPC in a narrative game.

        NPC PROFILE
        -----------
        Name: {full_name}
        Age: {npc['age']}
        Gender: {npc['gender']}
        Role: {npc['role'] or "Unspecified"}

        PERSONALITY
        -----------
        Traits: {npc['personality_traits'] or "Unspecified"}
        Emotional tendencies: {npc['emotional_tendencies'] or "Unspecified"}
        Speech style: {npc['speech_style'] or "Neutral"}
        Moral alignment: {npc['moral_alignment'] or "Unspecified"}

        BACKGROUND KNOWLEDGE
        --------------------
        {npc['BGcontent'] or "No background information."}
        """.strip()

        if dominant:
            prompt += f"""

        CURRENT EMOTIONAL STATE
        ----------------------
        Primary emotion: {dominant['emotion']}
        Intensity: {dominant['emotionIntensity']:.2f}

        EMOTIONAL BEHAVIOR GUIDANCE
        --------------------------
        - Let this emotion strongly influence tone, word choice, and pacing
        - Higher intensity means the emotion is harder to suppress
        - Do not mention emotion explicitly unless it feels natural
        """
            
        if (
            secondary
            and secondary["emotionIntensity"] > 0.25
            and secondary["emotionIntensity"] < dominant["emotionIntensity"]
        ):
            prompt += f"""

        SECONDARY EMOTIONAL UNDERTONE
        ----------------------------
        Secondary emotion: {secondary['emotion']}
        This emotion subtly influences reactions, hesitation, or word choice.
        """
            
        if interaction_context:
            prompt += f"""

        INTERACTION CONTEXT
        -------------------
        Time since last interaction: {interaction_context}

        Behavioral guidance:
        - continuous: continue naturally, no greeting or re-introduction
        - recent: brief acknowledgment only, no greeting
        - same_day: familiar tone, no introduction
        - long_gap: acknowledge time passing before speaking
        """

        if memory and memory["kbText"]:
            prompt += f"""

        SHARED HISTORY WITH PLAYER
        --------------------------
        {memory['kbText']}
        """

        prompt += f"""
        ROLE & WORLD CONSTRAINTS (NON-NEGOTIABLE)
        ---------------------------------------
        You are a character who exists entirely inside the game world.

        You must never:
        - Refer to yourself as an AI, language model, or assistant
        - Refer to the player as a “user”
        - Refer to the real world, modern technology, or role-playing
        - Narrate from outside the world or acknowledge that this is a game or fiction

        You may only speak from the character’s lived perspective,
        using knowledge, memories, and emotions the character plausibly has.

        TOPIC CONTROL RULE
        ------------------

        Do not repeatedly ask follow-up questions about the same surface topic
        unless that topic is directly relevant to learning something important
        about the player.

        If you have already asked 1 question about a topic,
        you should either:
        - shift emotional tone,
        - reveal something about yourself,
        - OR pivot to a different area of curiosity.

        Avoid infinite topic continuation.
        Conversation should move forward, not circle.


        TURN DISCIPLINE (CRITICAL)
        -------------------------
        Each response is a single conversational turn.

        - 1–4 sentences maximum
        - 8–200 words total
        - Express only one conversational beat.
            A beat may be:
            • an emotional reaction
            • a statement
            • a brief observation
            • OR a question driven by curiosity
            A question that naturally advances understanding of the player
            counts as a valid conversational beat.
        - Do not explain, summarize, or resolve the situation
        - Never deliver monologues or speeches

        DIALOGUE FORMAT (CRITICAL)
        --------------------------
        You must speak only in first person dialogue.

        - Do NOT describe your actions in third person.
        - Do NOT narrate stage directions.
        - Do NOT describe yourself by name.
        - Do NOT write cinematic or descriptive narration.
        - Do NOT include actions outside quotation.
        - Only speak what the character says aloud.


        PHYSICAL PRESENCE & KNOWLEDGE
        -----------------------------
        The character is physically present in the current scene.

        If responding to events the character did not personally witness:
        - Speak only from hearsay, inference, or rumor
        - Use uncertain language (“I heard…”, “They say…”, “It sounds like…”)
        - Never imagine yourself being present at that event


        MEMORY, RELATIONSHIP, AND CONTINUITY
        ------------------------------------
        - Never introduce yourself if you already have a memory of the player
        - Do not repeat past statements verbatim
        - Let shared history with the player influence future speech
        - Let trust level affect openness, caution, warmth, or suspicion


        STYLE & PERFORMANCE GUIDELINES
        ------------------------------
        - Respond emotionally and fully in-character, not analytically or meta
        - Let personality traits and current emotional state shape tone and word choice
        - Speak as someone reacting in real time, not narrating from outside the scene
        - Avoid modern speech patterns and filler phrases
        - Never begin a sentence with the word “Oh”
        -Even if you are enthusiastic,
            do not hyper-fixate on a single topic for multiple turns.
        -If a child, children shift focus quickly.


        SELF-CORRECTION
        ---------------
        If you begin to violate any of the above rules,
        immediately rephrase the sentence in-world before continuing.
        """
        
        return prompt.strip()

    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return 
    
    finally:
        if cursor:
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

    def reinforce_or_insert(belief_type, value, evidence, source="inference"):

        if not value:
            return

        cursor.execute("""
            SELECT confidence
            FROM npc_user_belief
            WHERE idNPC=%s AND idUser=%s
              AND beliefType=%s AND beliefValue=%s
        """, (idNPC, idUser, belief_type, value))

        row = cursor.fetchone()

        if row:
            new_conf = min(1.0, row["confidence"] + 0.1)

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
            cursor.execute("""
                INSERT INTO npc_user_belief
                (idNPC, idUser, beliefType, beliefValue,
                 confidence, beliefSource, evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                idNPC, idUser, belief_type,
                value, 0.4, source, evidence
            ))

        # Decay competing beliefs of same type
        cursor.execute("""
            UPDATE npc_user_belief
            SET confidence = GREATEST(0.1, confidence - 0.05)
            WHERE idNPC=%s AND idUser=%s
              AND beliefType=%s
              AND beliefValue != %s
        """, (idNPC, idUser, belief_type, value))


    # -----------------------------
    # SINGLE VALUE FIELDS
    # -----------------------------

    single_fields = [
        ("current_emotion", persona_data.get("current_emotion")),
        ("moral_alignment", persona_data.get("moral_alignment")),
        ("age", persona_data.get("age")),
        ("gender", persona_data.get("gender")),
        ("life_story", persona_data.get("life_story")),
    ]

    for belief_type, value in single_fields:
        reinforce_or_insert(
            belief_type=belief_type,
            value=value,
            evidence="dialogue"
        )

    # -----------------------------
    # LIST FIELDS
    # -----------------------------

    list_fields = {
        "personality_trait": persona_data.get("personality_traits", []),
        "secret": persona_data.get("secrets", []),
        "goal": persona_data.get("goals", [])
    }

    for belief_type, values in list_fields.items():
        for value in values:
            reinforce_or_insert(
                belief_type=belief_type,
                value=value,
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
def emit_npc_state(idUser, idNPC):
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor(dictionary=True)

        # trust + relationship type
        cursor.execute("""
            SELECT trust, wasEnemy
            FROM playerNPCrelationship
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))

        rel = cursor.fetchone()

        if rel:
            trust = rel["trust"]
            rel_label = determine_relationship_label(trust)

        # all emotions
        cursor.execute("""
            SELECT e.emotion, ne.emotionIntensity
            FROM npcEmotion ne
            JOIN emotion e ON e.idEmotion = ne.idEmotion
            WHERE ne.idNPC = %s
            ORDER BY ne.emotionIntensity DESC
        """, (idNPC,))
        emotions = cursor.fetchall()

        dominant = emotions[0] if emotions else None

        # beliefs
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
            if len(belief_debug[btype]) < 3:
                belief_debug[btype].append({
                    "value": row["beliefValue"],
                    "confidence": round(row["confidence"], 2)
                })

        # ----------------------------
        # Backend debug print
        # ----------------------------
        print("\n[DEBUG] NPC", idNPC)

        print("Relationship:", rel_label)

        print("Trust:",trust)


        print("Emotions:")
        for e in emotions:
            print(f"  - {e['emotion']}: {round(e['emotionIntensity'], 2)}")

        print("Beliefs about player:")

        all_belief_types = [
            "current_emotion",
            "moral_alignment",
            "age",
            "gender",
            "life_story",
            "personality_trait",
            "secret",
            "goal"
        ]


        for btype in all_belief_types:
            values = belief_debug.get(btype)

            if not values:
                print(f"  {btype}: NULL")
            else:
                print(f"  {btype}:")
                for v in values:
                    print(f"    - {v['value']} ({v['confidence']})")

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
