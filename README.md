Install nix 

`sh <(curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install) --no-daemon`

Enter nix 

`nix develop`

## Measurement

Bringing the rig up on a fresh host allocation, and the ordered queue of runs that
re-establishes the baseline: **[`docs/MIGRATION.md`](docs/MIGRATION.md)**. Start there —
the host configuration it lists is runtime state that does not survive a reboot, and
several of its steps fail in ways that read as success.

The running log of what has been measured, what was refuted, and what each number is
allowed to be compared against: [`docs/INVESTIGATION.md`](docs/INVESTIGATION.md).
