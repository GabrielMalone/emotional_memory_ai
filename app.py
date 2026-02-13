from flask import Flask, request, jsonify, send_file
from openai import OpenAI
from flask_cors import CORS
from dotenv import load_dotenv
from phase_1_queries import *
from phase_2_queries import *
from elevenlabsQueries import *
import openAIqueries
import os, uuid
from flask_socketio import SocketIO, join_room
import base64
import hashlib


AUDIO_DIR = "./tts_cache"
os.makedirs(AUDIO_DIR, exist_ok=True)
speechOn = False  # set to false to save 11 lab tokens


#------------------------------------------------------------------
# we need to have this API sit between Unreal and MYSQL Database
#------------------------------------------------------------------
load_dotenv()
camo = Flask(__name__)
CORS(camo)                      # allow anything to access this API
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
socketio = SocketIO(camo, cors_allowed_origins="*")


#------------------------------------------------------------------
# socket events
#------------------------------------------------------------------
@socketio.on("connect")
def onConnect():
    print("player connected")


@socketio.on("register_user")
def register_user(data):
    print('registering user')
    idUser = data["idUser"]
    join_room(f"user:{idUser}")

#------------------------------------------------------------------
# openAI Routes
#------------------------------------------------------------------
# helper method
def saveAudio(audio):
    audio = b"".join(audio)
    audio_id = str(uuid.uuid4())
    path = f"{AUDIO_DIR}/{audio_id}.mp3"
    with open(path, "wb") as f:
        f.write(audio)
    return {"audio_id": audio_id}, 200

#------------------------------------------------------------------
# NPC INTERACT HELPERS
#------------------------------------------------------------------

#------------------------------------------------------------------
# cache for 11 labs
#------------------------------------------------------------------
def tts_cache_key(text, voice_id, emotion):
    text = text.strip()
    raw = f"{voice_id}|{emotion}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
#------------------------------------------------------------------
def tts_cached(text, voice_id, emotion):
    key = tts_cache_key(text, voice_id, emotion)
    path = f"{AUDIO_DIR}/{key}.mp3"

    if os.path.exists(path):
        with open(path, "rb") as f:
            while True:
                chunk = f.read(32_768)  # 32KB
                if not chunk:
                    break
                print("USING CACHE!")
                yield chunk
        return

    audio_chunks = []
    for chunk in tts(text, voice_id, emotion):
        audio_chunks.append(chunk)
        yield chunk

    with open(path, "wb") as f:
        f.write(b"".join(audio_chunks))

        
def build_NPC_prompt(idUser, idNPC):
    prompt = build_prompt(idUser=idUser, idNPC=idNPC)
    return prompt

#------------------------------------------------------------------
def emit_npc_state(idUser, idNPC):
    db = connect()
    if not db.is_connected():
        return

    try:
        cursor = db.cursor(dictionary=True)

        # trust
        cursor.execute("""
            SELECT trust
            FROM playerNPCrelationship
            WHERE idUser = %s AND idNPC = %s
        """, (idUser, idNPC))
        rel = cursor.fetchone()

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

        socketio.emit(
            "npc_state_update",
            {
                "idNPC": idNPC,
                "trust": rel["trust"] if rel else None,
                "dominantEmotion": dominant["emotion"] if dominant else None,
                "dominantIntensity": dominant["emotionIntensity"] if dominant else None,
                "emotions": emotions,  
            },
            room=f"user:{idUser}"
        )

    finally:
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
# NPC INTERACT -- STREAM NPC OUTPUT AND UPDATE KB 
#------------------------------------------------------------------
@camo.route("/npc_interact", methods=["POST"])  
def npc_interact():
    SENTENCE_END = {".", "?", "!"}

    try:
        data     = request.json
        curScene = data["currentScene"]
        pName    = data["playerName"]
        idUser   = data["idUser"]
        idNPC    = data["idNPC"]
        idVoice  = data["idVoice"]
        pText    = data["playerText"]

        print(f"\nDATA: {data}\n")

        # ----------------------------------------------------------
        # decay emotions before applying new stimulus
        # ----------------------------------------------------------
        decay_rate = get_emotion_decay_rate(idNPC)
        decay_npc_emotions(idNPC=idNPC, decay=decay_rate)

        # ----------------------------------------------------------
        # classify player input
        # ----------------------------------------------------------
        raw_mem = get_mem(idNPC=idNPC, idUser=idUser)
        cls_mem = openAIqueries.build_classification_context(raw_mem)
        classification = openAIqueries.classify_player_input(
            data["playerText"], cls_mem, client
        )

        trust_delta  = classification["trust_delta"]
        emotion_name = classification["emotion"]
        offensive    = classification["offensive"]

        if offensive:
            update_NPC_user_memory_query(
                idNPC=idNPC,
                idUser=idUser,
                kbText="[player spoke offensively or disrespectfully]"
            )
            update_trust(idUser, idNPC, -50)

        update_trust(idUser, idNPC, trust_delta)

        reactivity = get_emotion_reactivity(idNPC)
        intensity  = min(1.0, classification["intensity"] * reactivity)

        # what should happen here really is we get NPC's current emotions and ask
        # based on the emotional context of what was just said 'emotion_name'
        # how does that interact with the current npc's emotions

        set_npc_emotion(idNPC, emotion_name, intensity)

        emit_npc_state(idUser, idNPC)

        # ----------------------------------------------------------
        update_NPC_user_memory_query(
            idNPC=idNPC,
            idUser=idUser,
            kbText=pText
        )

        prompt = build_prompt(idUser=idUser, idNPC=idNPC)

        full_text = []
        sentence_buffer = ""
        speaking_emitted = False

        # ----------------------------------------------------------
        # stream text + audio
        # ----------------------------------------------------------
        for token in openAIqueries.getResponseStream(
            prompt, curScene, pName, client
        ):
            full_text.append(token)
            sentence_buffer += token

            socketio.emit(
                "npc_text_token",
                {"token": token},
                room=f"user:{idUser}"
            )
            socketio.sleep(0)

            if (
                speechOn
                and sentence_buffer.strip()
                and sentence_buffer.strip()[-1] in SENTENCE_END
            ):
                if not speaking_emitted:
                    socketio.emit(
                        "npc_speaking",
                        {"idNPC": idNPC, "state": True},
                        room=f"user:{idUser}"
                    )
                    speaking_emitted = True

                for audio_chunk in tts_cached(
                    sentence_buffer, idVoice, emotion_name
                ):
                    payload = base64.b64encode(audio_chunk).decode("utf-8")
                    socketio.emit(
                        "npc_audio_chunk",
                        {"audio_b64": payload},
                        room=f"user:{idUser}"
                    )
                    socketio.sleep(0)

                sentence_buffer = ""

        # ----------------------------------------------------------
        # flush remaining text
        # ----------------------------------------------------------
        if speechOn and sentence_buffer.strip():
            if not speaking_emitted:
                socketio.emit(
                    "npc_speaking",
                    {"idNPC": idNPC, "state": True},
                    room=f"user:{idUser}"
                )
                speaking_emitted = True

            for audio_chunk in tts_cached(
                sentence_buffer, idVoice, emotion_name
            ):
                payload = base64.b64encode(audio_chunk).decode("utf-8")
                socketio.emit(
                    "npc_audio_chunk",
                    {"audio_b64": payload},
                    room=f"user:{idUser}"
                )
                socketio.sleep(0)

        # ----------------------------------------------------------
        # done sending
        # ----------------------------------------------------------
        socketio.emit("npc_text_done", {}, room=f"user:{idUser}")
        socketio.emit("npc_audio_done", {}, room=f"user:{idUser}")

        # ----------------------------------------------------------
        # update KB with NPC response
        # ----------------------------------------------------------
        text = "".join(full_text)
        kbText = f"[You just responded to {pName} with:] '{text}'"
        update_NPC_user_memory_query(
            idNPC=idNPC,
            idUser=idUser,
            kbText=kbText
        )

        return jsonify({"success": True}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
           
#------------------------------------------------------------------



#------------------------------------------------------------------
# PHASE 2 ROUTES
#------------------------------------------------------------------
@camo.route("/tts_audio/<audio_id>", methods=["GET"])
def tts_audio(audio_id):
    path = f"{AUDIO_DIR}/{audio_id}.mp3"
    return send_file(path, mimetype="audio/mp3")
#------------------------------------------------------------------
@camo.route("/match_choice", methods=["POST"])      # phase 2 query 
def match_choice():
    return openAIqueries.match_choice_query(client)
#------------------------------------------------------------------
@camo.route("/update_NPC_user_mem", methods=["POST"]) 
def update_NPC_user_memory():
    data    = request.json
    idNPC  = data["idNPC"] 
    idUser = data["idUser"]
    kbText = data["kbText"]
    return update_NPC_user_memory_query(idNPC=idNPC, idUser=idUser, kbText=kbText)
#------------------------------------------------------------------
@camo.route("/get_NPC_user_mem", methods=["POST"]) 
def get_NPC_user_memory():
    data    = request.json
    idNPC  = data["idNPC"] 
    idUser = data["idUser"]
    return get_NPC_user_memory_query(idUser=idUser, idNPC=idNPC)
#------------------------------------------------------------------
@camo.route("/get_inventory", methods=["POST"]) 
def get_inventory()->jsonify:
    data = request.json
    idUser = data["idUser"]
    return get_inventory_query(idUser=idUser)
#------------------------------------------------------------------
@camo.route("/get_avail_storylets", methods=["POST"]) 
def get_avail_storylets()->jsonify:
    data = request.json
    idUser = data["idUser"]
    idNPC = data["idNPC"]
    return get_avail_storylets_query(idUser=idUser, idNPC=idNPC)
#------------------------------------------------------------------
@camo.route("/get_NPC_BG", methods=["POST"]) 
def get_NPC_BG()->jsonify:
    data = request.json
    idNPC = data["idNPC"]
    print(f'get NPC BG INFO for idNPC: {idNPC}\n bg info: {data}')
    return get_NPC_BG_query(idNPC=idNPC)
#------------------------------------------------------------------
@camo.route("/get_rel_info_user_NPC", methods=["POST"])
def get_rel_info_user_NPC()->jsonify:
    data = request.json
    idUser = data["idUser"]
    idNPC = data["idNPC"]
    return get_user_NPC_rel_query(idUser=idUser, idNPC=idNPC)
#------------------------------------------------------------------
@camo.route("/get_NPC_emotion_info", methods=["POST"])
def get_NPC_emotion_info()->jsonify:
    data = request.json
    idNPC = data["idNPC"]
    return get_NPC_emotion_query(idNPC=idNPC)
#------------------------------------------------------------------
@camo.route("/init_user_NPC_rel", methods=["POST"])
def init_user_NPC_rel()->jsonify:
    data = request.json
    idNPC = data["idNPC"]
    idUser = data["idUser"]
    return init_user_NPC_rel_query(idNPC=idNPC, idUser=idUser)
#------------------------------------------------------------------
@camo.route("/get_storylet_choices", methods=["POST"])
def get_storylet_choices()->jsonify:
    data = request.json
    idStorylet = data["idStorylet"]
    return get_storylet_choices_query(idStorylet=idStorylet)
#------------------------------------------------------------------
@camo.route("/get_choice_content", methods=["POST"])
def get_choice_content()->jsonify:
    data = request.json
    idChoice = data["idChoice"]
    return get_choice_content_query(idChoice=idChoice)
#------------------------------------------------------------------
# phase 2 choices routes
#------------------------------------------------------------------
# 1 - Adwin becomes an acquaintence to user
@camo.route("/idChoice_1", methods=["POST"])
def idChoice_1()->jsonify:
    data = request.json
    idNPC = data["idNPC"]
    idUser = data["idUser"]
    idStorylet = data["idStorylet"]
    return idChoice_1_query(idNPC=idNPC, idUser=idUser, idStorylet=idStorylet)
#------------------------------------------------------------------
# 3 - offer to find flowers for Adwin
@camo.route("/idChoice_3", methods=["POST"])
def idChoice_3()->jsonify:
    data = request.json
    idNPC = data["idNPC"]
    idUser = data["idUser"]
    idStorylet = data["idStorylet"]
    return idChoice_3_query(idNPC=idNPC, idUser=idUser, idStorylet=idStorylet)
#------------------------------------------------------------------
@camo.route("/get_user_tasks", methods=["POST"])
def get_user_tasks():
    data = request.json
    idUser = data["idUser"]
    return get_user_tasks_query(idUser=idUser)

#------------------------------------------------------------------
# PHASE 1 ROUTES
#------------------------------------------------------------------
@camo.route("/load_KB", methods=["GET"]) 
def load_KB()->jsonify:
    return load_KB_Query(idUser=1)              # just id 1 for now
#------------------------------------------------------------------
@camo.route("/init_KB", methods=["POST"]) 
def init_KB()->jsonify:
    data    = request.json
    player  = data["player"]
    idNPC   = data["idNPC"] # not using this anymore just hardcoded
    return init_KB_query(idUser=1, player=player)   
#------------------------------------------------------------------
@camo.route("/append_to_KB", methods=["POST"]) 
def add_to_KB()->jsonify:
    data    = request.json
    npc_id  = data["idNPC"] # not using this anymore just hardcoded
    entry   = data["entry"]
    return append_KB_query(idUsesr=1, entry=entry)



if __name__ == "__main__":
    socketio.run(camo, host="0.0.0.0", port=5001, debug=True, use_reloader=True)