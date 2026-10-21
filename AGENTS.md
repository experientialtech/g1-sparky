# Agent notes

## Setting the Gemini API key

The robot's online mode needs a Gemini API key in `online/.env`. That file is gitignored. Never commit the key
or write it anywhere else.

```bash
[ -f online/.env ] || cp online/.env.example online/.env
# then set this line in online/.env (raw key, no quotes):
# GEMINI_API_KEY=<key>
python3 online/check_gemini.py   # must print "OK: Gemini Live is working"
```

If `online/.env` already exists, replace only the `GEMINI_API_KEY=` line.

Then start Rocky (voice + vision) with `./online/start_rocky.sh`. Full details: [online/README.md](online/README.md).
