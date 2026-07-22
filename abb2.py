#!/usr/bin/python3
import RPi.GPIO as GPIO
import time
from datetime import datetime
import json
import paho.mqtt.client as mqtt
import requests

# ==========================================
# PIN CONFIGURATION & SETUP
# ==========================================
SENSOR_PIN = 22

GPIO.setmode(GPIO.BCM)
GPIO.setup(SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ==========================================
# GLOBAL VARIABLES & COUNTERS
# ==========================================
# Shift Totals
run_time = loading_time = delay_time = downtime = 0.0
total_rest_time = planned_stop_time = model_change_time = total_machine_time = 0.0

# Real Operating Time (resets on shift end and model change)
real_operating_time = 0.0
# Total Real Operating Time (resets on shift end ONLY)
total_real_operating_time = 0.0
# Tracks active time for the current batch (resets on model change & shift end)
batch_run_time = 0.0 

# Output Trackers
total_output = 0            # Product count (resets on model change)
shift_total_output = 0      # Total product count (resets on shift end only)
hourly_output = 0 
total_rejects = 0           # Sum of dashboard rejects received via MQTT

# Hourly Trackers
hourly_rest_time = 0.0
lost_time_this_hour = 0.0 
base_time_this_hour = 0.0   # Accurately tracks base time even if model changes mid-hour

# Sensor & Cycle Trackers
sensor_blocked = True
blockage_start_time = 0.0
current_cycle_time = 0.0

# Non-blocking sensor stabilization
raw_sensor_state = 0
stable_sensor_state = 0
sensor_state_change_time = 0.0
STABILIZATION_TIME = 2.0

# Trigger to bypass LOADING state and jump straight to DELAY
force_delay = False

# ==========================================
# MQTT & GOOGLE SHEETS CONFIGURATION
# ==========================================
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_DATA = "sensor2/data"

# Node-RED Topics
MQTT_TOPIC_SHIFT_FORM = "nodered/newshift"
MQTT_TOPIC_SETUP = "nodered/modeltarget" 
MQTT_TOPIC_NR_REJECT = "nodered/reject"
MQTT_TOPIC_NR_DOWNTIME = "nodered/downtime"
MQTT_TOPIC_NR_ENDSHIFT = "nodered/endshift" 
MQTT_TOPIC_MODE = "nodered/mode"
MQTT_TOPIC_PARAM_CONDITION = "noderedparam/condition"

# NEW: Topic to handle manual count adjustments (+1 / -1)
MQTT_TOPIC_ADJUST_COUNT = "nodered/adjust_count"

# Live PRS dashboard topics
MQTT_TOPIC_LIVE_HOURLY = "smartchecksheet/hourly"
MQTT_TOPIC_LIVE_SNAPSHOT = "smartchecksheet/live"

# Google Sheets Web App URL
WEB_APP_URL = "https://script.google.com/macros/s/AKfycbyh8cXOCpU3TJciLMMzHVnZu5mE5gBypjRguNO8HDXKja3BU2qfw_S02zuRVbpjaAdyOw/exec"

# ==========================================
# STATE & SHIFT MANAGEMENT
# ==========================================
STATUS_RUN = 0
STATUS_LOADING = 1
STATUS_DELAY = 2
STATUS_REST = 3
STATUS_DOWN = 4
STATUS_PLANNED_STOP = 5
STATUS_MODEL_CHANGE = 6

MODE_NORMAL = "NORMAL"
MODE_DOWN = "DOWN"
MODE_MODEL_CHANGE = "MODEL CHANGE"
MODE_REST = "REST"
MODE_PLANNED_STOP = "PLANNED STOP"

current_mode = MODE_NORMAL
current_status = STATUS_RUN
previous_status = STATUS_RUN # For debug state change tracking
last_sent_hour = -1
last_reset_day = -1 # Tracks the last day an auto-reset occurred
last_live_snapshot_publish = 0.0

# Initialized with "NO PROD" so it displays correctly on startup
current_shift = {
    "shift_id": "NO PROD", "line": "NO PROD", "date": "NO PROD", "shift": "NO PROD", "group": "-",
    "model": "NO PROD", "lot_number": "NO PROD", "target": 0, "standard_cycle": 1.0,
    "supervisor": "-", "leader": "-", "workingTime": "-", 
    "forming": "-", "waterjet": "-", "assembly": "-", "quality": "-"
}

google_sheets_queue = [] 

# Helper to safely convert string payload numbers to integers
def safe_int(val):
    try: return int(val)
    except (ValueError, TypeError): return 0

def get_hour_slot(dt=None):
    if dt is None:
        dt = datetime.now()
    start_hour = dt.hour
    end_hour = (start_hour + 1) % 24
    return f"{start_hour}.00-{end_hour}.00"

def reset_shift_data():
    global run_time, loading_time, delay_time, downtime, total_rest_time, planned_stop_time, model_change_time
    global total_machine_time, total_output, hourly_output, hourly_rest_time, lost_time_this_hour, base_time_this_hour, sensor_blocked
    global shift_total_output, total_rejects, current_cycle_time, real_operating_time, total_real_operating_time
    global current_shift, batch_run_time 
    
    print("\n[EVENT] Executing Shift Data Reset...")
    run_time = loading_time = delay_time = downtime = 0.0
    total_rest_time = planned_stop_time = model_change_time = total_machine_time = 0.0
    total_output = hourly_output = shift_total_output = total_rejects = 0
    hourly_rest_time = lost_time_this_hour = base_time_this_hour = 0.0
    real_operating_time = total_real_operating_time = current_cycle_time = batch_run_time = 0.0
    sensor_blocked = True

    # Revert the shift details back to NO PROD
    current_shift = {
        "shift_id": "NO PROD", "line": "NO PROD", "date": "NO PROD", "shift": "NO PROD", "group": "-",
        "model": "NO PROD", "lot_number": "NO PROD", "target": 0, "standard_cycle": 1.0,
        "supervisor": "-", "leader": "-", "workingTime": "-", 
        "forming": "-", "waterjet": "-", "assembly": "-", "quality": "-"
    }

def execute_end_shift():
    # If there is no active shift, skip the PDF but STILL reset the timers
    if current_shift["shift_id"] == "NO PROD":
        print("\n[EVENT] No active shift. Skipping PDF generation, but resetting background timers.")
    else:
        print(f"\n[EVENT] End Shift Triggered!")
        print(f"[HTTP] Requesting PDF Generation for Shift: {current_shift['shift_id']}...")
        pdf_payload = {
            "action": "GENERATE_PDF",
            "shift_id": current_shift["shift_id"]
        }
        try:
            res = requests.post(WEB_APP_URL, json=pdf_payload, timeout=15)
            print(f"[HTTP] PDF Generation Response: {res.text}")
        except Exception as e:
            print(f"[HTTP ERROR] Failed to trigger PDF generation: {e}")
    
    # ALWAYS reset the data variables and timers at the end of this function
    reset_shift_data()

def push_hourly_to_sheets(is_model_change=False):
    global hourly_output, lost_time_this_hour, base_time_this_hour, total_output, hourly_rest_time, batch_run_time, real_operating_time
    
    std_cycle = current_shift.get("standard_cycle", 1.0)
    
    # Calculate exact available time for THIS specific model in THIS specific hour
    base_minutes = base_time_this_hour / 60.0
    lost_minutes = lost_time_this_hour / 60.0
    available_minutes = max(0.0, base_minutes - lost_minutes)
    
    plan_output = int(available_minutes / std_cycle) if std_cycle > 0 else 0
    now = datetime.now()
    
    if is_model_change:
        h_slot = get_hour_slot(now)
        reason = "MODEL CHANGE OVERRIDE"
    else:
        end_hour = now.hour
        start_hour = (end_hour - 1) % 24
        h_slot = f"{start_hour}.00-{end_hour}.00"
        reason = "STANDARD HOURLY PUSH"

    rest_mins = round(hourly_rest_time / 60.0, 2)
    
    row_data = [
        current_shift["shift_id"], current_shift["date"], current_shift["model"], 
        h_slot, plan_output, hourly_output, current_shift["lot_number"], rest_mins
    ]

    print(f"\n[EVENT - HOURLY CALCULATION] Reason: {reason}")
    print(f" +--> Slot: {h_slot} | Base Mins: {base_minutes:.2f} | Lost Mins: {lost_minutes:.2f} | Available: {available_minutes:.2f}")
    print(f" +--> Target: {plan_output} | Actual: {hourly_output} | Hourly Rest: {rest_mins}m")
    print(f" +--> QUEUING DATA: {row_data}")
    
    google_sheets_queue.append({"tab": "Hourly_Data", "row": row_data})

    # Publish the authoritative finalized row immediately for FlowFuse.
    hourly_event = {
        "shift_id": current_shift["shift_id"],
        "date": current_shift["date"],
        "line": current_shift["line"],
        "shift": current_shift["shift"],
        "model": current_shift["model"],
        "hour_slot": h_slot,
        "plan": plan_output,
        "actual": hourly_output,
        "lot_number": current_shift["lot_number"],
        "rest_time": rest_mins,
        "reason": reason,
        "finalized_at": datetime.now().isoformat()
    }
    try:
        mqtt_client.publish(
            MQTT_TOPIC_LIVE_HOURLY,
            json.dumps(hourly_event),
            qos=1
        )
        print(f"[MQTT] Published finalized hourly row to '{MQTT_TOPIC_LIVE_HOURLY}'.")
    except Exception as e:
        print(f"[MQTT ERROR] Failed to publish finalized hourly row: {e}")
    
    # Reset hourly trackers
    hourly_output = 0
    lost_time_this_hour = 0.0
    base_time_this_hour = 0.0
    hourly_rest_time = 0.0
    if is_model_change:
        print("[EVENT] Model Change Flag triggered -> Resetting Total Output and Real Operating Time to 0.")
        total_output = 0 
        batch_run_time = 0.0      # Reset batch timer for the new model
        real_operating_time = 0.0 # Reset real operating time for the new model

# ==========================================
# MQTT CALLBACKS
# ==========================================
def on_connect(client, userdata, flags, rc):
    client.subscribe([(MQTT_TOPIC_SHIFT_FORM, 0), (MQTT_TOPIC_SETUP, 0), 
                      (MQTT_TOPIC_NR_REJECT, 0), (MQTT_TOPIC_NR_DOWNTIME, 0), 
                      (MQTT_TOPIC_NR_ENDSHIFT, 0), (MQTT_TOPIC_MODE, 0),
                      (MQTT_TOPIC_PARAM_CONDITION, 0),
                      (MQTT_TOPIC_ADJUST_COUNT, 0)]) # <--- ADDED SUBSCRIPTION HERE
    print(f"\n[SYSTEM] Connected to MQTT Broker. Ready to receive commands.")

def on_message(client, userdata, msg):
    global current_mode, current_shift, total_rejects, force_delay
    global hourly_output, total_output, shift_total_output, current_cycle_time
    
    topic = msg.topic
    
    try:
        payload_str = msg.payload.decode('utf-8').strip()
        try: data = json.loads(payload_str); is_json = True
        except ValueError: data = payload_str; is_json = False

        if topic == MQTT_TOPIC_SHIFT_FORM and is_json:
            date_clean = data.get("prodDate", "").replace("-", "")
            current_shift["shift_id"] = f"{date_clean}-{data.get('shift', '')}-{data.get('productionLine', '').replace(' ', '')}"
            
            def parse_ops(op_data):
                if isinstance(op_data, list): return ", ".join(op_data)
                return str(op_data) if op_data else "-"

            current_shift.update({
                "date": data.get("prodDate", "-"),
                "line": data.get("productionLine", "-"),
                "shift": data.get("shift", "-"),
                "group": data.get("group", "-"),
                "workingTime": data.get("workingTime", "-"),
                "supervisor": data.get("supervisor", "-"),
                "leader": data.get("lineLeader", "-"),
                "forming": parse_ops(data.get("formingOperator")),
                "waterjet": parse_ops(data.get("waterjetOperator")),
                "assembly": parse_ops(data.get("assemblyOperator")),
                "quality": data.get("qualityOperator", "-")
            })

            print(f"\n[MQTT EVENT] New Shift Started: {current_shift['shift_id']}")
            row_data = [
                current_shift["shift_id"], current_shift["date"], current_shift["line"], 
                current_shift["shift"], current_shift["group"], current_shift["workingTime"],
                current_shift["supervisor"], current_shift["leader"],
                current_shift["forming"], current_shift["waterjet"],
                current_shift["assembly"], current_shift["quality"]
            ]
            print(f" +--> QUEUING SHIFT DATA: {row_data}")
            google_sheets_queue.append({"tab": "Shift_Data", "row": row_data})

        elif topic == MQTT_TOPIC_SETUP and is_json:
            print(f"\n[MQTT EVENT] Setup / Model Target Updated")
            if "model" in data: current_shift["model"] = data.get("model", "-")
            if "lot_number" in data: current_shift["lot_number"] = data.get("lot_number", "-")
            if "hourly_plan" in data:
                current_shift["target"] = data.get("hourly_plan", 0)
            elif "total_target" in data:
                current_shift["target"] = data.get("total_target", 0)
            if "standard_cycle" in data: current_shift["standard_cycle"] = float(data.get("standard_cycle", 1.0))
            print(f" +--> Current Model: {current_shift['model']} | Lot: {current_shift['lot_number']} | Cycle: {current_shift['standard_cycle']}")

        elif topic == MQTT_TOPIC_NR_REJECT and is_json:
            print(f"\n[MQTT EVENT] Reject Data Received")
            
            # 1. LIVE DASHBOARD CALCULATION: 
            new_dashboard_rejects = safe_int(data.get("totalRejectNG"))
            total_rejects += new_dashboard_rejects

            # 2. GOOGLE SHEETS (CHECKSHEET) DATA:
            row_data = [
                current_shift["shift_id"], get_hour_slot(), 
                data.get("totalSlabReject", ""), data.get("slabRejectCode", ""), 
                data.get("totalReturnRoll", ""), data.get("ohtNumber", ""), 
                data.get("totalRejectNG", ""), data.get("ngRejectCode", ""), 
                data.get("totalLoftLayerReject", ""), data.get("loftLayerRejectCode", "")
            ]
            print(f" +--> QUEUING REJECT: {row_data} | Running Total Rejects (Dashboard): {total_rejects}")
            google_sheets_queue.append({"tab": "Reject_Data", "row": row_data})

        elif topic == MQTT_TOPIC_NR_DOWNTIME and is_json:
            print(f"\n[MQTT EVENT] Downtime Data Received")
            if current_mode != MODE_NORMAL:
                print(f"\n[STATE CHANGE] Downtime logged. Auto-reverting mode: {current_mode} -> {MODE_NORMAL}")
                current_mode = MODE_NORMAL
                force_delay = True # TRIGGER: Auto-skip loading on next product detection
                
            row_data = [
                current_shift["shift_id"], get_hour_slot(), 
                data.get("category", ""), data.get("code", ""), 
                data.get("durationMinutes", ""), data.get("description", ""), data.get("remarks", "")
            ]
            print(f" +--> QUEUING DOWNTIME: {row_data}")
            google_sheets_queue.append({"tab": "Downtime_Data", "row": row_data})

        elif topic == MQTT_TOPIC_NR_ENDSHIFT:
            if isinstance(data, dict):
                is_end = data.get("value", False)
            else:
                is_end = data
                
            if is_end == True or str(is_end).lower() == "true":
                print(f"\n[MQTT EVENT] End Shift Requested from Node-RED!")
                execute_end_shift()

        elif topic == MQTT_TOPIC_PARAM_CONDITION and is_json:
            print(f"\n[MQTT EVENT] Parameter Condition Data Received")
            
            param_data = data.get("paramData", [{}])[0] if data.get("paramData") else {}
            temp_data = data.get("tempData", [{}])[0] if data.get("tempData") else {}
            glue_data = data.get("glueData", [{}])[0] if data.get("glueData") else {}
            
            model = param_data.get("model") or current_shift["model"]
            
            row_data = [
                current_shift["shift_id"], 
                model,
                param_data.get("heating", ""), 
                param_data.get("cooling", ""), 
                param_data.get("shuttle", ""), 
                param_data.get("waterjet", ""),
                temp_data.get("rh", ""), 
                temp_data.get("ctr", ""), 
                temp_data.get("lh", ""),
                glue_data.get("std", ""), 
                glue_data.get("act", "")
            ]
            
            print(f" +--> QUEUING PARAMETER DATA: {row_data}")
            google_sheets_queue.append({"tab": "Parameter_Data", "row": row_data})

        elif topic == MQTT_TOPIC_MODE:
            new_mode = str(data).upper()
            valid_modes = [MODE_NORMAL, MODE_DOWN, MODE_MODEL_CHANGE, MODE_REST, MODE_PLANNED_STOP]
            if new_mode in valid_modes:
                if new_mode == MODE_MODEL_CHANGE and current_mode != MODE_MODEL_CHANGE:
                    print("\n[EVENT] Model Change sequence initiated! Forcing hourly push...")
                    push_hourly_to_sheets(is_model_change=True)
                
                if current_mode != new_mode:
                    print(f"\n[STATE CHANGE] Machine Mode changed: {current_mode} -> {new_mode}")
                    
                    # --- NEW TRIGGER ---
                    # If returning to NORMAL from REST (or any other mode), arm the force_delay flag
                    if new_mode == MODE_NORMAL:
                        print("[EVENT] Returning to NORMAL mode. Next product detection will jump straight to DELAY.")
                        force_delay = True
                        
                    current_mode = new_mode

        # ==========================================
        # NEW BLOCK: ADJUST PRODUCT COUNT (+1 / -1)
        # ==========================================
        elif topic == MQTT_TOPIC_ADJUST_COUNT and is_json:
            adjust_val = safe_int(data.get("adjust", 0))
            print(f"\n[MQTT EVENT] Manual Count Adjustment Received: {adjust_val}")
            
            # Apply adjustment and ensure counts never go below 0
            hourly_output = max(0, hourly_output + adjust_val)
            total_output = max(0, total_output + adjust_val)
            shift_total_output = max(0, shift_total_output + adjust_val)
            current_cycle_time = (
                (real_operating_time / total_output) / 60.0
                if total_output > 0 else 0.0
            )
            
            print(f" +--> Corrected Counts -> Hourly: {hourly_output} | Total: {total_output} | Shift: {shift_total_output}")

    except Exception as e: 
        print(f"\n[ERROR] MQTT parsing failed: {e} | Payload: {msg.payload}")

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message 
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
def process_gsheets_queue():
    if len(google_sheets_queue) > 0:
        item = google_sheets_queue.pop(0)
        payload = {"action": "APPEND_ROW", "tab_name": item["tab"], "row_data": item["row"]}
        print(f"\n[HTTP] Attempting to push {item['tab']} data to Google Sheets...")
        try:
            requests.post(WEB_APP_URL, json=payload, timeout=5)
            print(f"[HTTP] SUCCESS! Data written to '{item['tab']}'.")
        except Exception as e: 
            print(f"[HTTP ERROR] Failed to send. Re-queuing data. Error: {e}")
            google_sheets_queue.insert(0, item) 

def publish_live_data():
    global last_live_snapshot_publish

    # Same available-time formula used by push_hourly_to_sheets().
    standard_cycle = current_shift.get("standard_cycle", 1.0)
    live_base_minutes = base_time_this_hour / 60.0
    live_lost_minutes = lost_time_this_hour / 60.0
    live_available_minutes = max(0.0, live_base_minutes - live_lost_minutes)
    live_plan = (
        int(live_available_minutes / standard_cycle)
        if standard_cycle > 0 else 0
    )
    full_hour_plan = int(60.0 / standard_cycle) if standard_cycle > 0 else 0

    payload = {
        "shift_id": current_shift["shift_id"],
        "hour_slot": get_hour_slot(),
        "line": current_shift["line"],
        "date": current_shift["date"],
        "shift": current_shift["shift"],
        "group": current_shift["group"],
        "working_time": current_shift["workingTime"],
        "supervisor": current_shift["supervisor"],
        "leader": current_shift["leader"],
        "forming_operator": current_shift["forming"],
        "waterjet_operator": current_shift["waterjet"],
        "assembly_operator": current_shift["assembly"],
        "quality_inspector": current_shift["quality"],
        "model": current_shift["model"],
        "lot_number": current_shift["lot_number"],
        "hourly_plan": current_shift.get("target", 0),
        "machine_status": current_status,
        "current_mode": current_mode,
        
        # New Output metrics
        "product_count": total_output,                     # Resets on model change
        "total_product_count": shift_total_output,         # Resets on shift end
        "hourly_output": hourly_output,
        "live_plan": live_plan,
        "full_hour_plan": full_hour_plan,
        "hour_base_minutes": round(live_base_minutes, 2),
        "hour_lost_minutes": round(live_lost_minutes, 2),
        "hour_available_minutes": round(live_available_minutes, 2),
        "total_reject": total_rejects,                     # Dashboard Rejects Only
        
        # Cycle metrics
        "current_cycle_time": round(current_cycle_time, 2), 
        "standard_cycle_time": current_shift.get("standard_cycle", 1.0),
        
        # Timing Metrics
        "real_operating_time": round(real_operating_time, 2), 
        "total_real_operating_time": round(total_real_operating_time, 2),
        "run_time": round(run_time, 2),
        "loading_time": round(loading_time, 2),
        "delay_time": round(delay_time, 2),
        "downtime": round(downtime, 2),
        "total_rest_time": round(total_rest_time, 2),
        "hourly_rest_time": round(hourly_rest_time, 2),
        "planned_stop_time": round(planned_stop_time, 2),
        "model_change_time": round(model_change_time, 2),
        "total_machine_time": round(total_machine_time, 2)
    }
    try:
        mqtt_client.publish(MQTT_TOPIC_DATA, json.dumps(payload))

        now = time.time()
        if now - last_live_snapshot_publish >= 1.0:
            mqtt_client.publish(
                MQTT_TOPIC_LIVE_SNAPSHOT,
                json.dumps(payload),
                qos=1,
                retain=True
            )
            last_live_snapshot_publish = now
    except Exception as e:
        print(f"[MQTT ERROR] Failed to publish live data: {e}")
    return payload

print("=====================================================")
print("  PRS Production Tracker Started (Verbose Debug Mode)")
print("=====================================================")

last_loop_time = time.time()
last_debug_print_time = time.time()

try:
    while True:
        current_time = time.time()
        loop_delta = current_time - last_loop_time
        last_loop_time = current_time
        
        now = datetime.now()

        # 1. Check for Standard Hourly Push (at minute 00)
        if now.minute == 0 and now.hour != last_sent_hour:
            push_hourly_to_sheets(is_model_change=False)
            last_sent_hour = now.hour

        # 1.5 NEW: Auto-Reset at exactly 8:00 AM (Checksheet Independent)
        if now.hour == 8 and now.minute == 0 and now.day != last_reset_day:
            print("\n[SYSTEM] 8:00 AM Reached. Forcing Auto-Reset (Checksheet independent)!")
            execute_end_shift()
            last_reset_day = now.day

        # 2. Process Data Queue
        process_gsheets_queue()

        # 3. State & Timer Logic
        base_time_this_hour += loop_delta
        total_machine_time += loop_delta
        
        # Only increment real operating time in NORMAL or DOWN modes
        if current_mode in [MODE_NORMAL, MODE_DOWN]:
            real_operating_time += loop_delta  
            total_real_operating_time += loop_delta

        if current_mode == MODE_NORMAL:
            # ---> NEW: Accumulate time for the batch average
            batch_run_time += loop_delta 
            
            # Non-blocking 2-second sensor stabilization.
            instant_state = 0 if GPIO.input(SENSOR_PIN) == 1 else 1

            if instant_state != raw_sensor_state:
                raw_sensor_state = instant_state
                sensor_state_change_time = current_time

            if (current_time - sensor_state_change_time) >= STABILIZATION_TIME:
                stable_sensor_state = raw_sensor_state

            current_raw_state = stable_sensor_state

            if current_raw_state == 1: # BLOCKED
                if not sensor_blocked:
                    hourly_output += 1
                    total_output += 1
                    shift_total_output += 1
                    
                    # Calculate cycle time in minutes
                    if total_output > 0:
                        current_cycle_time = (real_operating_time / total_output) / 60.0

                    sensor_blocked = True
                    blockage_start_time = current_time
                    print(f"\n[SENSOR] Product Detected! Hourly: {hourly_output} | Total (Model): {total_output} | Total (Shift): {shift_total_output} | Avg Cycle: {current_cycle_time:.2f}m")
                
                # --- APPLY FORCE DELAY FLAG ---
                if force_delay:
                    blockage_start_time = current_time - 95  # Backdate it so it's instantly > 90s
                    force_delay = False
                # ------------------------------
                
                duration = current_time - blockage_start_time
                if duration < 90:
                    current_status = STATUS_LOADING
                    loading_time += loop_delta
                else:
                    current_status = STATUS_DELAY
                    delay_time += loop_delta

            else: # UNBLOCKED
                sensor_blocked = False
                current_status = STATUS_RUN
                run_time += loop_delta

        else:
            # Handle non-normal modes
            if current_mode == MODE_DOWN:
                current_status = STATUS_DOWN
                downtime += loop_delta
                lost_time_this_hour += loop_delta
            elif current_mode == MODE_REST:
                current_status = STATUS_REST
                total_rest_time += loop_delta
                hourly_rest_time += loop_delta
                lost_time_this_hour += loop_delta
            elif current_mode == MODE_PLANNED_STOP:
                current_status = STATUS_PLANNED_STOP
                planned_stop_time += loop_delta
                lost_time_this_hour += loop_delta
            elif current_mode == MODE_MODEL_CHANGE:
                current_status = STATUS_MODEL_CHANGE
                model_change_time += loop_delta
                lost_time_this_hour += loop_delta

        # Check for Status Changes to print in Terminal
        if current_status != previous_status:
            status_names = {0: "RUN", 1: "LOADING", 2: "DELAY", 3: "REST", 4: "DOWN", 5: "PLANNED STOP", 6: "MODEL CHANGE"}
            print(f"\n[STATE CHANGE] Sensor/Machine Status changed: {status_names.get(previous_status)} -> {status_names.get(current_status)}")
            previous_status = current_status

        # 4. Publish Live Data & Verbose Terminal Print
        live_payload = publish_live_data()
        
        # Print a clean debug summary every 3 seconds so it doesn't flood the terminal instantly
        if current_time - last_debug_print_time >= 3.0:
            print(f"\n--- [LIVE TELEMETRY @ {datetime.now().strftime('%H:%M:%S')}] ---")
            print(f"Mode: {current_mode} | Status Code: {current_status}")
            print(f"Output -> Hourly: {hourly_output} | Model Total: {total_output} | Shift Total: {shift_total_output}")
            print(f"Cycles -> Current: {current_cycle_time:.2f}m | Standard: {current_shift.get('standard_cycle', 1.0)}m | Rejects: {total_rejects}")
            print(f"Timers -> Real Op Time: {real_operating_time:.1f}s | Total Real Op Time: {total_real_operating_time:.1f}s | Run: {run_time:.1f}s | Down: {downtime:.1f}s")
            print("---------------------------------------")
            last_debug_print_time = current_time
            
        time.sleep(0.05)

except KeyboardInterrupt: 
    print("\n\n[SYSTEM] Keyboard Interrupt Detected. Shutting down gracefully...")
finally: 
    GPIO.cleanup()
    mqtt_client.disconnect()
    mqtt_client.loop_stop()
    print("[SYSTEM] Exit complete.")


