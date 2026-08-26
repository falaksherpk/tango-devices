# `linac/` — Part 3: TANGO Controls Beamline System

This directory holds the Part 3 beamline device servers, replacing the old
`test/...` Tango domain from Phases 0-3 (Phase 1/2/3 files above this
directory are left in place, untouched -- they're historical record, not
moved or rewritten).

## Domain convention

Every device registers under `linac/<family>/<member>`, matching this
directory layout:

| Directory       | Tango device            | Chapter |
|-----------------|--------------------------|---------|
| `linac/magnet/`  | `linac/magnet/q1`        | Ch2 |
| `linac/camera/`  | `linac/camera/ceyag1`    | Ch3 |
| `linac/beam/`    | `linac/beam/faradaycup1` | Ch4 |
| `linac/vacuum/`  | `linac/vacuum/gauge1`    | Ch5 (evolved from Phase 2/3's ModbusVacuumController) |
| `linac/safety/`  | `linac/safety/interlock1`| Ch6 |

Full build plan: see `Part3_TANGO_Chap01_v1.0.md` onward.
