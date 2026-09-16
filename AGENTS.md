# im-hub

CLI-only local IM evidence hub. No frontend, no mandatory LLM, no message sending.

Preserve the deterministic core: source identity, parsing, ingestion, query, coverage and provenance must work without a model or remote service. Optional semantic analysis is a separate consumer of explicitly exported evidence.

Never commit actual messages, screenshots, databases, credentials, internal URLs, real account/group identifiers or machine-specific local config. Use synthetic fixtures only. Keep local data and acceptance probes in ignored `.local/` directories. Do not copy the former experiment directory wholesale.

Query must never drive a client, start collection, mutate the store, or send network requests. New acquisition is an explicit command. The configured-file collector is not an unattended UI driver. Optional native UI drivers require explicit --allow-ui and a calibrated profile; code and mocked tests are not live UI acceptance. Fail closed on identity conflicts and unknown scope; preserve incomplete and stale data labels.

Do not extract credentials or client memory, alter source databases, create schedules, log in new IM clients or automate sending as part of ordinary development. The legacy store is read-only through this package. MyMind formal state is not owned by im-hub.

Validation: `python -m unittest discover -s tests -v`; packaging: `python -m pip install -e .`; CLI: `im-hub --help` or `python -m im_hub --help`. ChatLab import integration uses pinned `chatlab-cli@0.37.1` via `IM_HUB_CHATLAB_DIR`, while querying already-imported stores does not require Node.
