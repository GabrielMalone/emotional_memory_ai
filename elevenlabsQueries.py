import os
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv
load_dotenv(".env")

# ----------------------------------------------------------------
# SPEECH TO TEXT (for human input)
# ----------------------------------------------------------------
def speech_to_text(wav_path: str) -> str:
    client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    with open(wav_path, "rb") as f:
        result = client.speech_to_text.convert(
            file=f,
            model_id="scribe_v2",   # ← correct param name
            language_code="eng",    # optional but recommended
        )

    return (result.text or "").strip()

# ----------------------------------------------------------------
# TEXT TO SPEECH (FOR NPC OUTPUT)
# ----------------------------------------------------------------
def tts(text, voice_id, emotion):

    client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

    EMOTION_CUES = {
        # core
        "neutral": "",
        "calm": "",

        # positive
        "happy": "[happily] ",
        "excited": "[excitedly] ",

        # negative
        "sad": "[sadly, tears welling] ",
        "angry": "[angrily, blood boiling] ",
        "afraid": "[fearfully] ",
        "disgusted": "[disgusted, nauseous] ",
    }

    cue = EMOTION_CUES.get(emotion, "")
    tagged_text = f"{cue}{text.strip()}"

    try:
        audio = client.text_to_dialogue.convert(
            inputs=[
                {
                    "text": tagged_text,
                    "voice_id": voice_id,
                }
            ]
        )

        if isinstance(audio, (bytes, bytearray)):
            yield audio
        else:
            # some SDKs return iterable chunks even without stream=True
            yield b"".join(audio)

    except Exception as e:
        print("ERROR ElevenLabs TTS:", e)
        raise