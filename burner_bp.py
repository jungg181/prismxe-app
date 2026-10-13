#!/usr/bin/env python3
"""Burner Turn On / Shut Off guided procedure (field tool, no login).

- GET /burner/           home: two big buttons (Start Up / Shut Off)
- GET /burner/startup    step-by-step startup procedure (12 steps, 2 x 30-min timers)
- GET /burner/shutdown   step-by-step shutdown procedure (6 steps)

All step content (EN + KO) is passed to the template as JSON; navigation,
timers and the alarm run client-side in vanilla JS.
"""
import json
from pathlib import Path

from flask import Blueprint, render_template

bp = Blueprint("burner", __name__, url_prefix="/burner",
               template_folder="templates/burner")

# ---------------------------------------------------------------- steps
# kind: "step" (plain instruction) or "timer" (countdown then auto-advance)
STARTUP_STEPS = [
    {"kind": "step",
     "title_en": "Check Valve Open/Close Status",
     "title_ko": "Valve Open/Close 상태 확인",
     "body_en": "Confirm the flow path is aligned correctly, for example that Flow Pump 1 is connected to Boiler 1.",
     "body_ko": "Flow Pump 1이 Boiler 1에 연결되어 있는지 등 유로 연결 상태가 올바른지 확인합니다."},
    {"kind": "step",
     "title_en": "Check HMI Alarms",
     "title_ko": "HMI Alarm 확인",
     "body_en": "Confirm that there is no active alarm on the HMI.",
     "body_ko": "HMI 화면에 활성 Alarm이 없는지 확인합니다."},
    {"kind": "step",
     "title_en": "Circulation Pump ON",
     "title_ko": "Circulation Pump ON",
     "body_en": 'Press the Circulation Pump "ON" button.',
     "body_ko": "순환 펌프 ON 버튼을 누릅니다."},
    {"kind": "step",
     "title_en": "FD Fan ON",
     "title_ko": "FD Fan ON",
     "body_en": 'Press the FD Fan "ON" button.',
     "body_ko": "FD Fan ON 버튼을 누릅니다."},
    {"kind": "step",
     "title_en": "FGR Fan ON",
     "title_ko": "FGR Fan ON",
     "body_en": 'Press the FGR Fan "ON" button.',
     "body_ko": "FGR Fan ON 버튼을 누릅니다."},
    {"kind": "step",
     "title_en": "Burner Start",
     "title_ko": "Burner Start",
     "body_en": 'After confirming all previous steps, press the "Burner Start" button.',
     "body_ko": "앞 단계 상태를 확인한 후 Burner Start 버튼을 누릅니다."},
    {"kind": "timer", "minutes": 30,
     "title_en": "Set MV to 20% — Run 30 Minutes",
     "title_ko": "MV 20% 설정 — 30분 운전",
     "body_en": "Set MV to 20% (* load factor) and run the boiler for 30 minutes. Start the timer below.",
     "body_ko": "MV를 20%로 설정하고 30분간 운전합니다. 아래 타이머를 시작하세요.",
     "done_en": "TIME'S UP — Set MV to 30% and continue.",
     "done_ko": "시간 종료 — MV를 30%로 설정하고 계속합니다."},
    {"kind": "timer", "minutes": 30,
     "title_en": "Set MV to 30% — Run 30 Minutes",
     "title_ko": "MV 30% 설정 — 30분 운전",
     "body_en": "Set MV to 30% and run the boiler for 30 minutes. Start the timer below.",
     "body_ko": "MV를 30%로 설정하고 30분간 운전합니다. 아래 타이머를 시작하세요.",
     "done_en": "TIME'S UP — Set MV to 40% and continue.",
     "done_ko": "시간 종료 — MV를 40%로 설정하고 계속합니다."},
    {"kind": "step",
     "title_en": "Set MV to 40% — Heat to 200°C",
     "title_ko": "MV 40% 설정 — 200°C까지 승온",
     "body_en": "Set MV to 40% and run until temperature reaches 200°C (392°F). Keep the rise at approx. 50°C (122°F) per hour. If it rises too fast, lower MV below 40% (e.g. 20% or 30%).",
     "body_ko": "MV를 40%로 설정하고 200°C(392°F)까지 운전합니다. 시간당 약 50°C(122°F) 상승을 유지하고, 너무 빠르면 MV를 40% 미만(예: 20% 또는 30%)으로 낮춥니다."},
    {"kind": "step",
     "title_en": "Switch to AUTO at 200°C",
     "title_ko": "200°C에서 AUTO 전환",
     "body_en": "At 200°C (392°F), turn the mode to AUTO. Set SP to 250°C (482°F) and monitor until the temperature reaches 250°C.",
     "body_ko": "200°C(392°F)에 도달하면 AUTO로 전환하고 SP를 250°C(482°F)로 설정한 뒤 250°C까지 모니터링합니다."},
    {"kind": "step",
     "title_en": "Check Flame Color",
     "title_ko": "화염 색상 확인",
     "body_en": "Check the flame through the burner sight glass (top or bottom). Orange = proper combustion. (Blue: excess oxygen · Red: insufficient oxygen · White: overheating)",
     "body_ko": "버너 Sight Glass(상단 또는 하단)로 화염 색상을 확인합니다. 주황색 = 적정 연소. (파랑: 산소 과다 · 빨강: 산소 부족 · 흰색: 과열)"},
    {"kind": "step",
     "title_en": "Check Stack Exhaust",
     "title_ko": "Stack 배기 확인",
     "body_en": "Check the stack exhaust. If it is black, turn the boiler off immediately.",
     "body_ko": "Stack 배기를 확인합니다. 검은색이면 Boiler를 즉시 정지합니다."},
]

SHUTDOWN_STEPS = [
    {"kind": "step",
     "title_en": "Burner Off",
     "title_ko": "Burner Off",
     "body_en": 'Press the "Burner Off" button.',
     "body_ko": "Burner Off 버튼을 누릅니다."},
    {"kind": "step",
     "title_en": "Keep Circulation Pump Running",
     "title_ko": "Circulation Pump 계속 운전",
     "body_en": "Keep the circulation pump running until the temperature reaches 120°C (248°F).",
     "body_ko": "온도가 120°C(248°F)에 도달할 때까지 순환 펌프를 계속 운전합니다."},
    {"kind": "step",
     "title_en": "Circulation Pump OFF",
     "title_ko": "Circulation Pump OFF",
     "body_en": "After reaching the target temperature, stop the circulation pump.",
     "body_ko": "목표 온도에 도달한 후 순환 펌프를 정지합니다."},
    {"kind": "step",
     "title_en": "FGR Fan OFF",
     "title_ko": "FGR Fan OFF",
     "body_en": "Stop the FGR Fan.",
     "body_ko": "FGR Fan을 정지합니다."},
    {"kind": "step",
     "title_en": "FD Fan OFF",
     "title_ko": "FD Fan OFF",
     "body_en": "Stop the FD Fan.",
     "body_ko": "FD Fan을 정지합니다."},
    {"kind": "step",
     "title_en": "Inspect for Abnormal Conditions",
     "title_ko": "이상 상태 점검",
     "body_en": "Check for oil leaks, abnormal vibration, unusual noise, or other abnormal conditions.",
     "body_ko": "누유, 비정상 진동, 이상 소음 등 비정상 상태가 없는지 확인합니다."},
]


@bp.get("/")
def home():
    return render_template("home.html")


@bp.get("/startup")
def startup():
    return render_template(
        "flow.html",
        flow_name="Burner Start Up",
        flow_name_ko="버너 가동",
        flow_id="startup",
        steps_json=json.dumps(STARTUP_STEPS, ensure_ascii=False),
    )


@bp.get("/shutdown")
def shutdown():
    return render_template(
        "flow.html",
        flow_name="Boiler Shut Off",
        flow_name_ko="보일러 정지",
        flow_id="shutdown",
        steps_json=json.dumps(SHUTDOWN_STEPS, ensure_ascii=False),
    )
