#!/bin/bash
# G1 Sparky - Stop all local services
echo "Stopping G1 Sparky..."
killall -9 llama-server whisper-server parecord 2>/dev/null
kill $(ps aux | grep conversation_pipeline | grep -v grep | awk "{print \$2}") 2>/dev/null
kill $(ps aux | grep power_widget | grep -v grep | awk "{print \$2}") 2>/dev/null
kill $(ps aux | grep power_monitor | grep -v grep | awk "{print \$2}") 2>/dev/null
echo "Stopped."
