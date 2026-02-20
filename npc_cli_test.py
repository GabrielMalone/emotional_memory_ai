import requests # type: ignore
import socketio # type: ignore
import base64
import sys
import threading
import time

from streamingMP3Player import StreamingMP3Player
from voiceRecorder import AudioRecorder
from elevenlabsQueries import speech_to_text

# --------------------------------------------------
# turn-taking state
# --------------------------------------------------
npc_is_speaking = threading.Event() #thread safe boolean flag
npc_is_speaking.clear()

SERVER = "http://localhost:5001"

player = None
#only one thread can read write player at the same time
player_lock = threading.Lock() 

# --------------------------------------------------
# audio drain callback
# --------------------------------------------------
def on_audio_drain():
    def delayed_release():
        global player
        time.sleep(0.35)  # post-speech grace period
            # so we dont pick up npc voice in user mic
        print("🎤 NPC finished speaking (audio drained)")
        npc_is_speaking.clear()

        with player_lock:
            player = None

    threading.Thread(target=delayed_release, daemon=True).start()

# -----------------------------
# config
# -----------------------------
idUser = 1
idNPC = 2
currentScene = """
    Adwin is looking for a place to sit at lunch at his new school. 
"""
playerName = "Gabriel"
voiceId = "SOYHLrjzK2X1ezoPC6cr"

# -----------------------------
# socket setup
# -----------------------------
sio = socketio.Client()

@sio.event
def connect():
    print("\n🔌 Connected to socket server\n")
    sio.emit("register_user", {"idUser": idUser})

# ---- AUDIO (transport only)
@sio.on("npc_audio_chunk")
def on_audio_chunk(data):
    with player_lock:
        if player is None:
            return

        chunk = base64.b64decode(data["audio_b64"])
        player.feed(chunk)

@sio.on("npc_audio_done")
def on_audio_done(_data=None):
    with player_lock:
        if player is None:
            return

        print("\n📦 Server finished sending audio\n")
        player.feed(None)

# ---- TEXT
@sio.on("npc_text_token")
def on_text_token(data):
    print(data["token"], end="", flush=True)

@sio.on("npc_text_done")
def on_text_done(_data=None):
    print("\n")

# ---- STATE
@sio.on("npc_state_update")
def on_npc_state(data):
    pass
   

# ---- TURN CONTROL (speech START only)
@sio.on("npc_speaking")
def on_npc_speaking(data):
    global player

    if not data.get("state"):
        return

    print("🗣️ NPC speaking")

    with player_lock:
        player = StreamingMP3Player()
        player.on_drain = on_audio_drain

    npc_is_speaking.set()

# -----------------------------
# connect socket
# -----------------------------
sio.connect(SERVER)

# -----------------------------
# main loop
# -----------------------------
recorder = AudioRecorder()

payload = {
    "idUser": idUser,
    "idNPC": idNPC,
    "currentScene": currentScene,
    "playerName": playerName,
    "idVoice": voiceId,
    "playerText": "<<<player has just arrived or returned, check your memory>>>"
}
# have NPC speak first
r = requests.post(f"{SERVER}/npc_interact", json=payload)

while True:
    try:

        while npc_is_speaking.is_set():
            time.sleep(0.05)


        # uncomment to use STT

        # wav_path = recorder.record()
        # print("Recorded:", wav_path)
        # user_text = speech_to_text(wav_path)
        # print("STT:", user_text)

        user_text = input("\nSay something...  ")
        print()
    

        payload = {
            "idUser": idUser,
            "idNPC": idNPC,
            "currentScene": currentScene,
            "playerName": playerName,
            "idVoice": voiceId,
            "playerText": "[player responded to you] " + user_text
        }

        r = requests.post(f"{SERVER}/npc_interact", json=payload)

        if not r.ok:
            print("❌ npc_interact failed", r.text)

    except KeyboardInterrupt:
        print("\n👋 Exiting")
        sio.disconnect()
        sys.exit(0)