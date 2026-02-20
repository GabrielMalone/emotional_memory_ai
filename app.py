from flask import Flask, request, jsonify, send_file
from openai import OpenAI
from flask_cors import CORS
from dotenv import load_dotenv
from phase_2_queries import *
from elevenlabsQueries import *
import openAIqueries
import os, uuid
from flask_socketio import SocketIO, join_room
import base64
import hashlib
import logging

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

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
#------------------------------------------------------------------
def saveAudio(audio):
    audio = b"".join(audio)
    audio_id = str(uuid.uuid4())
    path = f"{AUDIO_DIR}/{audio_id}.mp3"
    with open(path, "wb") as f:
        f.write(audio)
    return {"audio_id": audio_id}, 200

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

        # update mem on what player just said

        update_NPC_user_memory_query(
            idNPC=idNPC,
            idUser=idUser,
            kbText=pText
        )

        # ----------------------------------------------------------
        # decay emotions before applying new stimulus
        # ----------------------------------------------------------
        decay_rate = get_emotion_decay_rate(idNPC)
        decay_npc_emotions(idNPC=idNPC, decay=decay_rate)

        # ----------------------------------------------------------
        # classify player input
        # ----------------------------------------------------------
        raw_mem = get_mem(idNPC=idNPC, idUser=idUser)
        cls_mem = openAIqueries.build_classification_context(raw_mem, 6)

        classification = openAIqueries.classify_player_input(
            data["playerText"], raw_mem, client, idNPC, idUser
        )

        trust_delta  = classification["trust_delta"]
        offensive    = classification["offensive"]

        # update NPC mem about this classification ? 

        kbText = f"[Player {pName}'s last statement {pText} was classified as:] {classification}"
        update_NPC_user_memory_query(
            idNPC=idNPC,
            idUser=idUser,
            kbText=kbText
        )

        print(f"\nNPC MEM UPDATE: {kbText}\n")

        # update trust from player's last output

        update_trust(idUser, idNPC, trust_delta)

        if offensive:
            update_NPC_user_memory_query(
                idNPC=idNPC,
                idUser=idUser,
                kbText="[player spoke offensively or disrespectfully]"
            )
            update_trust(idUser, idNPC, -50)
        

        # extract npcs's beliefs about player based on this last output from player

        beliefs = openAIqueries.extract_persona_clues(
            player_text=pText,
            recent_context=cls_mem,
            client=client,
            idNPC=idNPC,
            idUser=idUser
        )

        print(f"\nEXTRACTED BELIEFS: {beliefs}\n")

        update_npc_user_beliefs(
            idNPC=idNPC,
            idUser=idUser,
            persona_data=beliefs
        )
 
        # ----------------------------------------------------------

        prompt = build_prompt(idUser=idUser, idNPC=idNPC)

        full_text = []
        sentence_buffer = ""
        speaking_emitted = False

        # ----------------------------------------------------------
        # stream text + audio
        # ----------------------------------------------------------
        db = connect()
        cursor = db.cursor(dictionary=True)
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
                    sentence_buffer, idVoice, dominant
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
                sentence_buffer, idVoice, dominant
            ):
                payload = base64.b64encode(audio_chunk).decode("utf-8")
                socketio.emit(
                    "npc_audio_chunk",
                    {"audio_b64": payload},
                    room=f"user:{idUser}"
                )
                socketio.sleep(0)

    

        # ----------------------------------------------------------
        # update KB with NPC response
        # ----------------------------------------------------------
        text = "".join(full_text)
        kbText = f"[You just responded to {pName} with:] '{text}'"

        print(f"\nNPC RESPONSE: {kbText}\n")

        update_NPC_user_memory_query(
            idNPC=idNPC,
            idUser=idUser,
            kbText=kbText
        )

        emotion_data = openAIqueries.classify_npc_reaction(pText, text, idNPC, idUser, client)
        reactivity = get_emotion_reactivity(idNPC)
        emotion_name = emotion_data["emotion"]
        base_intensity = emotion_data["intensity"]

        reactivity = get_emotion_reactivity(idNPC)
        intensity = min(1.0, base_intensity * reactivity)

        set_npc_emotion(idNPC, emotion_name, intensity)

        updated_mem = get_mem(idNPC=idNPC, idUser=idUser)
        classification = openAIqueries.classify_player_input(
            data["playerText"], updated_mem, client, idNPC, idUser
        )

        self_beliefs = openAIqueries.extract_self_beliefs(text, classification, client, idNPC)
        openAIqueries.merge_self_beliefs(idNPC, self_beliefs["beliefs"])
  

        # ----------------------------------------------------------
        # done sending
        # ----------------------------------------------------------
        socketio.emit("npc_text_done", {}, room=f"user:{idUser}")
        socketio.emit("npc_audio_done", {}, room=f"user:{idUser}")
        emit_npc_state(idUser, idNPC, socketio)

        return jsonify({"success": True}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
           


#------------------------------------------------------------------
if __name__ == "__main__":
    socketio.run(camo, host="0.0.0.0", port=5001, debug=False, use_reloader=True)