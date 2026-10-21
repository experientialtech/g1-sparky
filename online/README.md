# G1 Online Mode (Gemini Live)

The robot talks and sees through Google's Gemini Live API. Audio comes in from the webcam mic over PulseAudio,
camera frames stream from `/dev/video0`, and replies play on every USB speaker.

## Add the API key

Everything reads the key from `online/.env`. That file is gitignored. Never commit it, and never put the key
in any other file.

1. Create the env file from the template, if it doesn't exist yet:

   ```bash
   cp online/.env.example online/.env
   ```

2. Open `online/.env` and set the `GEMINI_API_KEY` line. Use the raw key: no quotes, no spaces around `=`.

   ```
   GEMINI_API_KEY=your-key-here
   ```

   If `online/.env` already exists, only change that one line and keep the other settings.

3. Check the key works:

   ```bash
   python3 online/check_gemini.py
   ```

   `OK: Gemini Live is working` means you're done. `FAIL: ...` says what's wrong: missing file, empty key,
   key rejected, or no internet.

## Run

```bash
pip install -r online/requirements.txt   # first time only
./online/start_rocky.sh                  # Rocky: party animal, voice + vision
```

Other personas use the scripts directly:

| Command | Persona | Camera |
|---|---|---|
| `./online/start_rocky.sh` | Rocky (party animal, snarky hype man) | yes |
| `COMPANY_PROFILE=ROCKY python3 online/g1_sparky.py` | Rocky | no |
| `python3 online/g1_sparky_vision.py` | Sparky (roast comic) | yes |
| `python3 online/g1_sparky.py` | Sparky | no |

The personas live in `online/prompts.py`. If the camera can't be opened, the vision script keeps running voice-only.

## Settings (`online/.env`)

| Variable | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | (required) | Google Gemini API key |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Gemini Live model |
| `GEMINI_LIVE_VOICE` | `Fenrir` | Voice. Only Gemini's 30 standard voices work in Live (Puck, Charon, Fenrir, Orus, Kore, ...). Voices from the larger library such as `en-us-*` are accepted but ignored. |
| `GEMINI_LIVE_TEMPERATURE` | `1.2` | Higher = wilder replies |
| `GEMINI_LIVE_SILENCE_MS` | `500` | Silence (ms) before the robot replies. Raise it to 650-800 if it cuts people off. |
| `IMAGE_SEND_INTERVAL` | `1.0` | Seconds between camera frames (Gemini accepts at most 1 frame per second) |
| `COMPANY_PROFILE` | `SPARKY` / `SPARKY_VISION` | Persona to load from `prompts.py` |

Tone, energy, and accent come from the persona prompt, not from an API setting. Rocky's deep voice comes from
a line in his prompt. Without it, excited delivery pushes every voice up into a high register.

## How it behaves

- **Turn-taking:** Gemini decides when you've finished talking. The mic is muted while the robot speaks and
  for 1.5 s afterwards, so it doesn't hear itself through the speakers. That also means you can't talk over it.
- **Connection drops:** Google recycles Live connections every few minutes. The scripts reconnect on their own
  and resume the same conversation. If the saved session has expired (for example after a network outage),
  they start a fresh conversation. They retry forever, backing off up to 30 s between attempts.
- **Camera:** frames are sent once per second while connected. If the camera can't be opened, the vision
  script runs voice-only.

## Files

| File | Purpose |
|---|---|
| `live_chat.py` | Shared voice loop: mic, speakers, Gemini Live session, reconnects |
| `g1_sparky.py` | Voice-only entry point |
| `g1_sparky_vision.py` | Voice + camera entry point |
| `prompts.py` | Personas (Sparky, Rocky, and their `_VISION` versions) |
| `start_rocky.sh` | Starts Rocky with voice + vision |
| `check_gemini.py` | Checks that the API key in `online/.env` works |
| `.env.example` | Template for `online/.env` |
