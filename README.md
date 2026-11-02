# tango-devices

The Tango Controls device servers of the beamline lab, their protocol
simulators and the Sardana controllers (handbook Part 3, Chapters 23-32;
packaged in Part 4, from Chapter 33). Python package `beamline_tango`,
distribution `beamline-tango-devices`.

## Layout

| Path | What |
|---|---|
| `src/beamline_tango/devices/` | one module per device server; the module name is the Tango server name |
| `src/beamline_tango/simulators/` | the fake instruments, on `127.0.0.1` or ptys only |
| `src/beamline_tango/sardana_controllers/` | Sardana controllers, the Pool's `PoolPath` (no `__init__.py`: the Pool imports every `.py` file here) |
| `tests/` | the test suites, one directory per device, plus `sardana/` (live, against the fleet) |
| `tools/` | manual clients and one-off proofs, not part of the package |
| `taurus_gui/` | the operator panel, run on `beamlinehost` from a checkout (not packaged) |

| Server | Tango device | Protocol |
|---|---|---|
| `magnet_power_supply/q1` | `linac/magnet/q1` | RS-232 |
| `ceyag_camera/ceyag1` | `linac/camera/ceyag1` | TCP/IP |
| `faraday_cup/faradaycup1` | `linac/beam/faradaycup1` | GPIB via Prologix |
| `vacuum_gauge/gauge1` | `linac/vacuum/gauge1` | Modbus TCP (registers) |
| `interlock_monitor/interlock1` | `linac/safety/interlock1` | Modbus TCP (coils) |

A server starts as `python3 -m beamline_tango.devices.<module> <instance>`;
Tango takes the server name from the module's file name.

## Tests

`python3 -m pytest tests/vacuum/ tests/safety/ tests/magnet/ tests/camera/ tests/beam/`
(the `process=False` suites first). `pyproject.toml` puts `src/` on the path.

## Packaging

Two Debian packages for Ubuntu 24.04 (handbook Part 4, Ch33), built from one commit:

- `beamline-tango-devices` (amd64): the device servers and Sardana controllers in a
  self-contained venv, `/opt/beamline-tango`, from the hash-locked
  `requirements/runtime.txt`; systemd template units for the servers and for Sardana's
  Pool and MacroServer (`tango-magnet@q1.service`, ..., `tango-pool@linac.service`),
  `tango-lab.target`, and the readiness checks in `/usr/libexec/beamline-tango/`.
- `beamline-tango-simulators` (all): the fake instruments and their template units
  (`tango-sim-*@.service`). A site with real instruments installs only the first.

Installing them enables and starts nothing. Each instance reads its site data from
`/etc/beamline-tango/<unit>-<instance>.env`; what a device starts after comes from
drop-ins. Both are written by beamline-ansible's `tango_ds` role.

```
podman build -t localhost/beamline-tango-deb-builder:noble packaging/deb
packaging/deb/build-deb.sh OUTDIR     # from HEAD, in a throwaway container
packaging/deb/test-deb.sh OUTDIR      # install into a fresh ubuntu:24.04, run the tests
```

To change a runtime version: edit `requirements/runtime.in`, re-resolve and re-hash
`requirements/runtime.txt` (one wheel per package, CPython 3.12, x86_64), and test.

## License

MIT, see `LICENSE`.
