from phase_1_queries import *
from openai import OpenAI
import json
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
def match_choice_query(client):

    try:
        data = request.json

        # data comes in looking like:
        # {'choices': [{'choiceText': 
        # 'Respond to Adwin’s introduction in a positive manner.', 'idChoice': 1}]}
        # so new now have a python dictionary with keys choices and playerText
        # get won't throw an exception if choices missing, instead set to empty list

        choices = data["choices"]
        playerText = data["playerText"]
        NPCoutput = data["NPCoutput"]

        print(f"choices: {choices} | playerText {playerText} | NPCoutput {NPCoutput}")

        # build a readable choice list for the model :
        # iterate the dictionaries in the choices array
        # place the corresponding values via the keys idChoice and choiceText
        # creating one big string with join
        
        choices_text = "\n".join(f"{c['idChoice']}: {c['choiceText']}" for c in choices)


        print(f"\n choices for openAI: {choices_text}\n")

        # we want a simple cheap model / no creative responses so gpt4 mini
        # this defines the conversation context the model sees.
        response = client.chat.completions.create(
            model="gpt-4.1",  
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a classifier for a narrative game.\n"
                        "Only match a choice if the player's reply is a CLEAR, DIRECT response\n"
                        "AND explicitly relates to one of the available choices\n"
                        "and is not just a reply to what the NPC just said.\n"
                        "Otherwise, return null.\n"
                        "If multiple choices seem relevant, return null.\n\n"
                        "Return ONLY valid JSON:\n"
                        "{ \"matchedChoiceId\": number | null }"
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"NPC just said:\n{NPCoutput}\n\n"
                        f"Player reply:\n{playerText}\n\n"
                        f"Available choices:\n{choices_text}"
                    )
                }
            ],
            # keep things as deterministic as possible
            temperature=0
        )
        raw = response.choices[0].message.content.strip()
        # Safety: ensure valid JSON
        # turn the openAI response string  
        # "{ \"matchedChoiceId\": number | null }" into pure JSON
        result = json.loads(raw) 
        print(f" openAI results: {result}\n")
        return jsonify({
            "matchedChoiceId": result.get("matchedChoiceId")
        })
    except Exception as e:
        print("MATCH ERROR:", e)
        return jsonify({"matchedChoiceId": None})
#------------------------------------------------------------------
def build_classification_context(raw_mem: str, max_entries: int = 6):
    """
    Build a neutral, factual summary of recent NPC↔player interaction
    for intent classification context.
    """
    if not raw_mem:
        return None

    # split + clean
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
# this needs to be updated to contain some recent conversation context 
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
    # inject recent interaction summary (if present)
    if mem and "recent_summary" in mem:
        system += (
            "\nRecent interaction summary:\n"
            f"{mem['recent_summary']}\n"
            "Use this to help give context the player's latest response.\n"
        )

    user = f"""
    Player text:
    \"\"\"{player_text}\"\"\"

    Classify with EXACTLY these fields and values:

    - sentiment: one of [positive, neutral, negative, hostile, affectionate]
    - intensity: number from 0.0 to 1.0
    - offensive: true or false
    - trust_delta: integer from -10 to +10
    - emotion: one of [happy, sad, angry, afraid, calm, excited, disgusted]

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

    if result["emotion"] not in ALLOWED_EMOTIONS:
        print(f"[WARN] Invalid emotion '{result['emotion']}', defaulting to 'calm'")
        result["emotion"] = "calm"

    print (f"recent memory: {mem}, \n{player_text}, \n{resp.choices[0].message.content}")

    return json.loads(resp.choices[0].message.content)