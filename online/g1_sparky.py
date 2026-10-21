#!/usr/bin/env python3
"""
G1 Voice Chat - voice only (Gemini Live)
Persona comes from COMPANY_PROFILE in online/.env (default: SPARKY).
"""

import live_chat

if __name__ == "__main__":
    live_chat.run(default_profile="SPARKY")
