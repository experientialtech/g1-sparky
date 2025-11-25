#!/bin/bash
# Real-time power monitor for Thor on G1 battery
# Logs power draw every second to file and stdout
LOG_FILE="${1:-/tmp/power_log.csv}"
echo "timestamp,gpu_mV,gpu_mA,gpu_W,cpu_soc_mV,cpu_soc_mA,cpu_soc_W,sys5v_mV,sys5v_mA,sys5v_W,total_uW,total_W" > "$LOG_FILE"
echo "Power monitor started -> $LOG_FILE"
while true; do
    ts=$(date +%H:%M:%S)
    gv=$(cat /sys/class/hwmon/hwmon5/in1_input 2>/dev/null || echo 0)
    gc=$(cat /sys/class/hwmon/hwmon5/curr1_input 2>/dev/null || echo 0)
    gw=$(echo "scale=1; $gv * $gc / 1000000" | bc 2>/dev/null || echo 0)
    cv=$(cat /sys/class/hwmon/hwmon5/in2_input 2>/dev/null || echo 0)
    cc=$(cat /sys/class/hwmon/hwmon5/curr2_input 2>/dev/null || echo 0)
    cw=$(echo "scale=1; $cv * $cc / 1000000" | bc 2>/dev/null || echo 0)
    sv=$(cat /sys/class/hwmon/hwmon5/in3_input 2>/dev/null || echo 0)
    sc=$(cat /sys/class/hwmon/hwmon5/curr3_input 2>/dev/null || echo 0)
    sw=$(echo "scale=1; $sv * $sc / 1000000" | bc 2>/dev/null || echo 0)
    tp=$(cat /sys/class/hwmon/hwmon6/power1_input 2>/dev/null || echo 0)
    tw=$(echo "scale=1; $tp / 1000000" | bc 2>/dev/null || echo 0)
    echo "$ts,$gv,$gc,$gw,$cv,$cc,$cw,$sv,$sc,$sw,$tp,$tw" >> "$LOG_FILE"
    printf "\r[%s] GPU:%5sW  CPU/SOC:%5sW  5V:%5sW  TOTAL:%5sW  " "$ts" "$gw" "$cw" "$sw" "$tw"
    sleep 1
done
