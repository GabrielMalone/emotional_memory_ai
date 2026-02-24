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
#------------------------------------------------------------------
from threading import Thread
from threading import Lock

#------------------------------------------------------------------
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

AUDIO_DIR = "./tts_cache"
os.makedirs(AUDIO_DIR, exist_ok=True)
speechOn = False  # set to false to save 11 lab tokens

memory_worker_lock = Lock()
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

        # ----------------------------------------------------------
        # 1. Decay existing emotions
        # ----------------------------------------------------------
        decay_rate = get_emotion_decay_rate(idNPC)
        decay_npc_emotions(idNPC=idNPC, decay=decay_rate)

        # ----------------------------------------------------------
        # 2. Classify player input
        # ----------------------------------------------------------
        raw_mem = get_mem(idNPC=idNPC, idUser=idUser)

        classification = openAIqueries.classify_player_input(
            pText,
            raw_mem,
            client,
            idNPC,
            idUser
        )

        trust_delta = classification["trust_delta"]
        offensive   = classification["offensive"]

        # ----------------------------------------------------------
        # 3. Update trust
        # ----------------------------------------------------------
        update_trust(idUser, idNPC, trust_delta)

        if offensive:
            update_trust(idUser, idNPC, -50)

        # ----------------------------------------------------------
        # 4. Extract beliefs about player
        # ----------------------------------------------------------
        beliefs = openAIqueries.extract_persona_clues(
            player_text=pText,
            recent_context=raw_mem,
            client=client,
            idNPC=idNPC,
            idUser=idUser
        )

        update_npc_user_beliefs(
            idNPC=idNPC,
            idUser=idUser,
            persona_data=beliefs
        )

        # Insert player turn immediately so prompt can see it

        #should include extracted beliefs about player int this update, oops
        insert_memory_buffer(
            idNPC=idNPC,
            idUser=idUser,
            playerText=pText,
            npcText=None,
            npcEmotion=None,
            npcIntensity=None,
            selfBeliefs=None,
            playerBeliefs=beliefs,
            playerOutputClassifiedAs=classification
        )

        # ----------------------------------------------------------
        # 6. Build prompt using updated memory - NPC OUTPUT
        # ----------------------------------------------------------
        prompt = build_prompt(idUser=idUser, idNPC=idNPC)

        # ----------------------------------------------------------
        # 6a. Stream Output w/ audio (get emotion for flavor)
        # ----------------------------------------------------------

        full_text = []
        sentence_buffer = ""
        speaking_emitted = False

        db = connect()
        cursor = db.cursor(dictionary=True)

        cursor.execute("""
            SELECT e.emotion, ne.emotionIntensity
            FROM npcEmotion ne
            JOIN emotion e ON e.idEmotion = ne.idEmotion
            WHERE ne.idNPC = %s
            ORDER BY ne.emotionIntensity DESC
        """, (idNPC,))
        emotions = cursor.fetchall()

        dominant = emotions[0] if emotions else None
        cursor.close()
        db.close()


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

        # Flush remaining audio
        if speechOn and sentence_buffer.strip():
            if not speaking_emitted:
                socketio.emit(
                    "npc_speaking",
                    {"idNPC": idNPC, "state": True},
                    room=f"user:{idUser}"
                )

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
        # 8. Final NPC response text
        # ----------------------------------------------------------
        npc_text = "".join(full_text)
        print(f"\nNPC RESPONSE: {npc_text}\n")

        # ----------------------------------------------------------
        # 9. Classify NPC emotional reaction
        # ----------------------------------------------------------
        emotion_data = openAIqueries.classify_npc_reaction(
            pText,
            npc_text,
            idNPC,
            idUser,
            client
        )

        base_intensity = emotion_data["intensity"]
        reactivity = get_emotion_reactivity(idNPC)
        intensity = min(1.0, base_intensity * reactivity)

        set_npc_emotion(idNPC, emotion_data["emotion"], intensity)

        # ----------------------------------------------------------
        # 10. Extract and merge self beliefs
        # ----------------------------------------------------------
        raw_mem = get_mem(idUser=idUser, idNPC=idNPC)
        latest_scene = openAIqueries.get_most_recent_scene(raw_mem)
        recent_convo = get_buffered_convo(idUser=idUser, idNPC=idNPC)

        latest_scene = latest_scene[0] if isinstance(latest_scene, tuple) else latest_scene
        recent_convo = recent_convo or ""
        latest_scene = latest_scene or ""

        self_beliefs = openAIqueries.extract_self_beliefs(
            npc_text,
            latest_scene + "\n" + recent_convo,
            client,
            idNPC
        )

        openAIqueries.merge_self_beliefs(
            idNPC,
            self_beliefs["beliefs"]
        )

        # ----------------------------------------------------------
        # 11. UPDATE MEMORY WITH NPC TURN (SECOND PHASE)
        # ----------------------------------------------------------
        insert_memory_buffer(
            idNPC=idNPC,
            idUser=idUser,
            playerText=None,
            npcText=npc_text,
            npcEmotion=emotion_data.get("emotion"),
            npcIntensity=round(emotion_data.get("intensity", 0), 2),
            selfBeliefs=self_beliefs.get("beliefs")
        )
        # Trigger structured memory consolidation asynchronously
        Thread(
            target=background_update_structured_kbtext,
            args=(idNPC, idUser),
            daemon=True
        ).start()

        # ----------------------------------------------------------
        # 12. Emit final state
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
def background_update_structured_kbtext(idNPC: int, idUser: int):
    """
    Continuously processes unprocessed exchanges
    until none remain.
    """

    # Prevent multiple workers running simultaneously
    if not memory_worker_lock.acquire(blocking=False):
        return  # Another worker is already running

    try:
        while True:
            processed = process_one_exchange(idNPC, idUser)
            if not processed:
                break  # No more exchanges left
    finally:
        memory_worker_lock.release()
#------------------------------------------------------------------
def process_one_exchange(idNPC: int, idUser: int) -> bool:
    print(f"\n[MEMORY WORKER] Updating memory for NPC {idNPC}, User {idUser}")

    db = connect()
    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM npc_user_memory_buffer
        WHERE idNPC = %s
          AND idUser = %s
          AND processed = 0
        ORDER BY createdAt ASC
    """, (idNPC, idUser))

    rows = cursor.fetchall()

    if not rows:
        cursor.close()
        db.close()
        return False  # nothing to process

    player_text = None
    npc_text = None
    npc_emotion = None
    npc_intensity = None
    buffer_ids = []

    for r in rows:

        if r.get("playerText") and player_text is None:
            player_text = r["playerText"]
            buffer_ids.append(r["idBuffer"])
            continue

        if r.get("npcText") and player_text is not None:
            npc_text = r["npcText"]
            npc_emotion = r.get("npcEmotion")
            npc_intensity = r.get("npcIntensity")
            buffer_ids.append(r["idBuffer"])
            break

    if not player_text or not npc_text:
        cursor.close()
        db.close()
        return False  # incomplete exchange

    kbtext_current = get_mem(idNPC=idNPC, idUser=idUser)

    relevant_self_beliefs = get_self_beliefs_snapshot(idNPC)
    relevant_player_beliefs = get_player_beliefs_snapshot(idNPC, idUser)

    updated_kb = openAIqueries.update_structured_kbtext(
        client=None,
        idUser=idUser,
        idNPC=idNPC,
        kbtext_current=kbtext_current,
        player_text=player_text,
        npc_text=npc_text,
        player_cls=None,
        npc_reaction={
            "emotion": npc_emotion,
            "intensity": npc_intensity
        },
        relevant_self_beliefs=relevant_self_beliefs,
        relevant_player_beliefs=relevant_player_beliefs,
    )

    overwrite_NPC_user_memory(
        idNPC=idNPC,
        idUser=idUser,
        kbText=updated_kb
    )

    placeholders = ",".join(["%s"] * len(buffer_ids))

    cursor.execute(f"""
        UPDATE npc_user_memory_buffer
        SET processed = 1,
            processedAt = NOW()
        WHERE idBuffer IN ({placeholders})
    """, tuple(buffer_ids))

    db.commit()
    cursor.close()
    db.close()

    print(f"[MEMORY WORKER] Processed exchange for User {player_text} \n NPC {npc_text}")

    return True  # successfully processed


if __name__ == "__main__":

    socketio.run(camo, host="0.0.0.0", port=5001, debug=False, use_reloader=True)