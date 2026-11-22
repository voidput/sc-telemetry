"""Synthetic TelemetryStreamResponse frames for UI dev when the game isn't streaming."""
import math
import random
import time

# mutable mock vehicle state that the fake trigger backend can flip (server --mock only)
STATE = {"lights_enabled": True, "gear_deployed": False, "shield_generators": True}


def frame(api, types, t: float):
    """Build one plausible frame at time t (seconds)."""
    r = api.TelemetryStreamResponse()
    s = math.sin

    # actor
    v = r.actor.vitals
    v.health.current, v.health.max = 80 + 20 * s(t / 9), 100
    v.oxygen.current, v.oxygen.max = 70 + 30 * s(t / 13), 100
    v.hunger.current, v.hunger.max = 62, 100
    v.thirst.current, v.thirst.max = 48, 100
    v.body_temperature.current, v.body_temperature.max = 37 + 0.6 * s(t / 7), 42
    v.heart_rate = 78 + 25 * (0.5 + 0.5 * s(t / 5))
    v.gravity = 1.0 + 3.5 * max(0.0, s(t / 4))
    v.pressure = 1.0
    r.actor.environment.external_temperature = -40 + 10 * s(t / 20)
    r.actor.environment.ambient_radiation_rate = max(0.0, 2.5 * s(t / 11))
    r.actor.environment.breathable_atmosphere = False
    r.actor.state.locomotion = types.ACTOR_LOCOMOTION_STATE_SEATED
    r.actor.state.crew_role = types.ACTOR_CREW_ROLE_PILOT
    r.actor.state.seat_name = "Pilot Seat"
    r.actor.state.uec_balance = 1_284_550

    # vehicle
    vs = r.vehicle.state
    vs.name, vs.class_name, vs.manufacturer = "Mock Gladius", "AEGS_Gladius", "Aegis Dynamics"
    vs.type = types.VEHICLE_TYPE_SPACESHIP
    vs.lights_enabled = STATE["lights_enabled"]
    vs.health = 0.75 + 0.25 * s(t / 17)
    vs.doors.open_state = types.VehicleDoorState.DOOR_OPEN_STATE_CLOSED
    vs.doors.lock_state = types.VehicleDoorState.DOOR_LOCK_STATE_LOCKED
    vs.shields.level = 0.5 + 0.5 * s(t / 6)
    vs.shields.faces.extend([max(0.0, min(1.0, 0.6 + 0.4 * s(t / 3 + k))) for k in range(4)])
    vs.master_mode.mode = types.VEHICLE_MASTER_MODE_SCM if int(t / 15) % 2 else types.VEHICLE_MASTER_MODE_NAVIGATION
    vs.master_mode.spooled = True
    for f in ("powerplants", "engines", "weapons", "coolers", "life_support", "gravity_generators",
              "sensors", "shield_generators", "shield_emitters"):
        setattr(vs.power, f, True)
    vs.power.shield_generators = STATE["shield_generators"]
    vs.power.gravity_generators = int(t / 10) % 3 != 0
    A = types.VehicleAnnunciator
    states = [A.ANNUNCIATOR_STATE_NOMINAL] * 10
    states[9] = A.ANNUNCIATOR_STATE_MAJOR_FLASHING if int(t) % 20 < 3 else A.ANNUNCIATOR_STATE_NOMINAL
    states[4] = A.ANNUNCIATOR_STATE_MINOR_ON if int(t) % 30 < 8 else A.ANNUNCIATOR_STATE_NOMINAL
    for name, st in zip(("pwr", "wpn", "thr", "shld", "cool", "qdrv", "radr", "prox", "hit", "misl"), states):
        setattr(vs.annunciators, name, st)
    fl = vs.flight
    fl.state.is_landed = False
    fl.state.gear = (types.VEHICLE_COMPONENT_DEPLOYMENT_STATE_DEPLOYED if STATE["gear_deployed"]
                     else types.VEHICLE_COMPONENT_DEPLOYMENT_STATE_RETRACTED)
    ins = fl.instruments
    ins.speed = 180 + 120 * s(t / 4)
    ins.hydrogen_fuel = max(0.0, 1 - (t % 300) / 300)
    ins.quantum_fuel = 0.82
    ins.g_meter.current, ins.g_meter.max = v.gravity, 9.0
    ins.boost.engaged = s(t / 4) > 0.6
    ins.boost.output = max(0.0, s(t / 4))
    ins.boost.reserves = 0.5 + 0.5 * s(t / 8)
    ins.altitude = 12000 + 3000 * s(t / 30)
    ins.radar_altitude = ins.altitude - 200
    ins.vertical_speed_indicator = 50 * s(t / 6)
    ins.atmospheric_density = 0.02
    ins.heading = (t * 12) % 360
    st = fl.settings
    st.ifcs_coupled.enabled = st.ifcs_coupled.active = int(t / 25) % 2 == 0
    st.ifcs_g_safe.enabled = st.ifcs_g_safe.active = True
    st.ifcs_esp.enabled = st.ifcs_esp.active = True
    st.ifcs_speed_limiter.enabled = st.ifcs_speed_limiter.active = True
    st.ifcs_proximity_assist.enabled = True

    # weapons
    w = r.weapons
    w.preset_index, w.preset_name = 0, "Guns"
    for i in range(3):
        ws = w.weapons.add(name=f"Weapon {i + 1}", is_powered=True, ready_to_fire=(int(t * 4) + i) % 5 != 0,
                           contained_in_preset=True)
    C = types.Countermeasures
    w.countermeasures.add(type=C.COUNTERMEASURE_TYPE_DECOY, amount=24 - int(t / 10) % 24)
    w.countermeasures.add(type=C.COUNTERMEASURE_TYPE_NOISE, amount=6)

    # events (rare)
    if random.random() < 0.01:
        E = types.GameEvent
        r.events.events.add(type=random.choice([E.GAME_EVENT_TYPE_ACTOR_ENTER_SEAT, E.GAME_EVENT_TYPE_ACTOR_LEAVE_SEAT,
                                                E.GAME_EVENT_TYPE_ACTOR_DOWNED]))

    # force feedback
    ff = r.force_feedback
    ff.gunfire = max(0.0, s(t * 3)) if int(t) % 6 < 2 else 0.0
    ff.stick_shaker = max(0.0, (v.gravity - 3.5) / 2)
    for ax, k in (("x", 1), ("y", 2), ("z", 3)):
        getattr(ff.angular, ax).resistance = 0.5 + 0.5 * s(t / k)
        getattr(ff.linear, ax).damping = 0.5 + 0.5 * s(t / (k + 1))

    r.turret.light_amplification = False
    return r


def now():
    return time.monotonic()
