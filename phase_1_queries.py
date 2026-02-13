from flask import request, jsonify
import os
import mysql.connector
from kb_init import kb_init_string
from datetime import datetime

#------------------------------------------------------------------
# will use this for KB datetime stamps
# entry = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Emory {kb_text}" 
#------------------------------------------------------------------

#------------------------------------------------------------------
def connect()->object:
    return mysql.connector.connect(
        user=os.getenv('DB_USER'), 
        password=os.getenv('DB_PASSWORD'), 
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST', 'localhost') )
#------------------------------------------------------------------
def load_KB_Query(idUser: int) -> jsonify:
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor(dictionary=True)
        query = """
            SELECT kbText, updatedAt
            FROM npc_user_memory
            WHERE idNPC = (SELECT idNPC FROM NPC WHERE nameFirst = 'Emory'
            )
            AND idUser = %s;
        """
        cursor.execute(query, (idUser,))
        row = cursor.fetchone()

        return jsonify(row), 200

    except mysql.connector.Error as err:
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
# create backstory for a new player for Emory's KB
def init_KB_query(idUser: int, player: str) -> jsonify:
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor()

        kb_entry = kb_init_string(player)

        query = """
            INSERT INTO npc_user_memory (idNPC, idUser, kbText)
            VALUES (
                (SELECT idNPC FROM NPC WHERE nameFirst = 'Emory'),
                %s,
                %s
            )
            ON DUPLICATE KEY UPDATE
              kbText = VALUES(kbText);
        """

        cursor.execute(query, (idUser, kb_entry))
        db.commit()
        return jsonify({"status": "ok"}), 200

    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------
def append_KB_query(idUser: int, entry: str) -> jsonify:
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor()

        query = """
            INSERT INTO npc_user_memory (idNPC, idUser, kbText)
            VALUES (
                (SELECT idNPC FROM NPC WHERE nameFirst = 'Emory'),
                %s,
                %s
            )
            ON DUPLICATE KEY UPDATE
            kbText = CONCAT(
                IFNULL(kbText, ''),
                '\\n\\n[',
                NOW(),
                '] ',
                VALUES(kbText)
            );
        """

        cursor.execute(query, (idUser, entry))
        db.commit()
        return jsonify({"status": "ok"}), 200

    except mysql.connector.Error as err:
        db.rollback()
        print("MySQL Error:", err)
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        db.close()
#------------------------------------------------------------------