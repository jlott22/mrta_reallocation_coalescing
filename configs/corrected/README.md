# Corrected experiment configurations

- `completed/` contains immutable records of the finished AGX v7 execution.
  Do not relaunch those campaigns.
- `pilot/` contains corrected pilot records, including rejected candidate rates
  and W5 conditions used to select the final 0.075/0.30/0.60 and B4/W10 design.
  They are provenance, not active settings.
- `diagnostics/` contains engineering-only probes that are never result rows.
- `hardware_optional_retry_14.json` is the only optional publication execution
  config. It is checksum-bound to the 82-success/14-failure checkpoint.
- `agx_board_bindings.example.json` documents the local binding schema. Actual
  bindings stay ignored under `configs/local/`.

The final design is summarized in `experiment_matrix.json`. No config in this
directory should be pooled with material recovered from deleted historical
campaigns.
