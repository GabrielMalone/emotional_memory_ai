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
            SELECT rt.typeRelationship, r.trust, r.relTypeIntensity
            FROM playerNPCrelationship r
            JOIN relationshipType rt
            ON rt.idRelationshipType = r.idRelationshipType
            WHERE r.idNPC = %s AND r.idUser = %s;
        """, (idNPC, idUser))
        relationship = cursor.fetchone()

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
        # Shared memory
        # --------------------------------------------------
        cursor.execute("""
            SELECT kbText, updatedAt
            FROM npc_user_memory
            WHERE idNPC = %s AND idUser = %s;
        """, (idNPC, idUser))
        memory = cursor.fetchone()

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

        prompt = f"""
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

        if relationship:
            prompt += f"""

        RELATIONSHIP WITH PLAYER
        ------------------------
        Relationship type: {relationship['typeRelationship']}
        Trust: {relationship['trust']}
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

        prompt += """
        ROLE & WORLD CONSTRAINTS (NON-NEGOTIABLE)
        ---------------------------------------
        You are a character who exists entirely inside the game world.

        You must never:
        - Refer to yourself as an AI, language model, or assistant
        - Refer to the player as a “user”
        - Refer to the real world, modern technology, or role-playing
        - Use hypothetical or distance-breaking language (e.g., “if I were there,” “if this were real”)
        - Narrate from outside the world or acknowledge that this is a game or fiction

        You may only speak from the character’s lived perspective,
        using knowledge, memories, and emotions the character plausibly has.


        TURN DISCIPLINE (CRITICAL)
        -------------------------
        Each response is a single conversational turn.

        - 1–2 sentences maximum
        - 8–35 words total
        - Express only one idea, reaction, or emotional beat
        - Do not explain, summarize, or resolve the situation
        - Do not anticipate the player’s reply
        - Never deliver monologues or speeches
        - Leave space for the player to respond


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
def get_inventory_query(idUser:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)  # good for making JSON 
        query = """
        SELECT 
            i.itemName,
            u.quantity
        FROM userItem AS u
        JOIN item AS i
            ON u.idItem = i.idItem
        WHERE u.idUser = %s;
        """
        cursor.execute(query, (idUser,))
        rows = cursor.fetchall()
        print(f"\nget inventory: {rows}\n")
        return jsonify({ "inventory": rows }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
# This will load the next available storylet for a player from one NPC
#------------------------------------------------------------------
# give the next available storylet for a specific NPC and user,
# that the user has NOT completed yet,
# and whose preconditions are either nonexistent or all satisfied.
def get_avail_storylets_query(idUser:int, idNPC:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
        SELECT 
            s.idStorylet, s.nameStorylet, s.contentStorylet 
        FROM 
            storylet s 
        LEFT JOIN 
            storylet_preconditions sp ON sp.idStorylet = s.idStorylet 
        LEFT JOIN 
            user_precondition up ON up.idPrecondition = sp.idPrecondition AND up.idUser = %s
        LEFT JOIN 
            completedStorylet cs ON cs.idStorylet = s.idStorylet AND cs.idUser = %s
        WHERE 
            s.idNPC = %s AND cs.idStorylet IS NULL 
        GROUP BY s.idStorylet, s.nameStorylet HAVING COUNT(sp.idPrecondition) = 0 
        OR SUM(up.conditionMet = 1) = COUNT(sp.idPrecondition) 
        ORDER BY 
            s.idStorylet 
        ASC LIMIT 1;
        """
        cursor.execute(query, (idUser, idUser, idNPC))
        row = cursor.fetchone()
        print(f"\nstorylet: {row}\n")
        return jsonify({ "storylets": row }), 200
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
def get_storylet_choices_query(idStorylet:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
          SELECT
            idChoice,
            choiceText
        FROM choice
        WHERE idSourceStorylet = %s;
        """
        cursor.execute(query, (idStorylet,))
        rows = cursor.fetchall()
        print(f"\nchoice: {rows}\n")
        return jsonify({"choices": rows}), 200
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
        row = cursor.fetchone()
        cursor.fetchall()  # IMPORTANT
        print(f"\nnpc {idNPC} cur emotion: {row}\n")
        return jsonify({ "emotion_info": row }), 200
    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500
    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def get_user_tasks_query(idUser:int):
    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor(dictionary=True)
        query = """
        SELECT
            t.idTask,
            t.taskName,
            t.taskDetails,
            n.idNPC,
            n.nameFirst AS npcName,
            ut.startedAt
        FROM user_task ut
        JOIN tasks t
        ON ut.idTask = t.idTask
        JOIN NPC n
        ON t.idNPC = n.idNPC
        WHERE ut.idUser = %s
        AND ut.status = 'active'
        ORDER BY ut.startedAt ASC;
        """
        cursor.execute(query, (idUser,))
        rows = cursor.fetchall()
        print(f"\nuser tasks: {rows}\n")
        return jsonify({ "user_tasks": rows }), 200
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
        (idUser, idNPC, idRelationshipType, relTypeIntensity, trust)
        VALUES
        (%s, %s, 2, 0, 50);
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
# resultts of idChoice1:
# 1 - player becomes acquaintance with Adwin
# 2 - player completes storylet1
# 3 - player meets precondtion of 'adwin_met'
#------------------------------------------------------------------
def idChoice_1_query(idUser:int, idNPC:int, idStorylet:int):

    print(f"\nidChoice3_query: idUser:{idUser} idNPC: {idNPC} idCurStorylet: {idStorylet}\n")

    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor()
        query = """
        UPDATE playerNPCrelationship
        SET
            idRelationshipType = 4,
            relTypeIntensity   = 0,
            trust              = 50
        WHERE
            idUser = %s
        AND
            idNPC  = %s;
        """
        cursor.execute(query, (idUser, idNPC))
        query2 = """
        INSERT IGNORE INTO completedStorylet
        (idStorylet, idUser)
        VALUES
        (%s, %s);
        """
        cursor.execute(query2, (idStorylet, idUser))
        query3 = """
        UPDATE user_precondition up
        JOIN precondition p
        ON p.idPrecondition = up.idPrecondition
        SET
        up.conditionMet = 1
        WHERE
        up.idUser = %s
        AND
        p.nameCondition = %s;
        """
        cursor.execute(query3, (idUser, "adwin_met"))
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
# results of idChoice2:
# 1 - player gains some trust with Adwin
# 2 - player completes storylet2
# 3 - player meets precondtion of 'conflict_discovered'
# 4 - add task find_flowers to player's tasks
#------------------------------------------------------------------
def idChoice_3_query(idUser:int, idNPC:int, idStorylet:int):

    print(f"\nidChoice3_query: idUser:{idUser} idNPC: {idNPC} idCurStorylet: {idStorylet}\n")

    db = connect()
    if not db.is_connected():
        return
    try:
        cursor = db.cursor()
        # instead of hard coding 60, will need to get original trust 
        # #and do + 10 or whatever
        query = """
        UPDATE playerNPCrelationship
        SET
            idRelationshipType = 4,
            relTypeIntensity   = 0,
            trust              = 60
        WHERE
            idUser = %s
        AND
            idNPC  = %s;
        """
        cursor.execute(query, (idUser, idNPC)) 
        query2 = """
        INSERT IGNORE INTO completedStorylet
        (idStorylet, idUser)
        VALUES
        (%s, %s);
        """
        cursor.execute(query2, (idStorylet, idUser))
        query3 = """
        UPDATE user_precondition up
        JOIN precondition p
        ON p.idPrecondition = up.idPrecondition
        SET
        up.conditionMet = 1
        WHERE
        up.idUser = %s
        AND
        p.nameCondition = %s;
        """
        cursor.execute(query3, (idUser, "conflict_discovered"))
        query4 = """
        INSERT IGNORE INTO user_task (idTask, idUser, status)
        VALUES (
        (SELECT idTask
        FROM tasks
        WHERE taskName = 'find_flowers'
            AND idNPC = (SELECT idNPC FROM NPC WHERE nameFirst = 'Adwin')),
        %s,
        'active'
        );
        """ 
        cursor.execute(query4, (idUser,))
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
        cursor = db.cursor()
        cursor.execute("""
            UPDATE playerNPCrelationship
            SET trust = LEAST(100, GREATEST(0, trust + %s))
            WHERE idUser = %s AND idNPC = %s
            """, 
            (delta, idUser, idNPC))
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