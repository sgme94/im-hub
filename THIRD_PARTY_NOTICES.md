# Provenance and third-party dependencies

The Python implementation and bounded codecs were migrated from the user's locally developed IM Unified CLI experiment. The selected codec code was written for that experiment; no complete Tencent client, decrypted database, credential extractor or external exporter is bundled.

The optional import backend is the separately installed **ChatLab CLI 0.37.1**. This repository does not redistribute ChatLab or its `node_modules`; consult the upstream project and installed package for their license and notices. The QCE compatibility parser consumes a reviewed export representation; neither QCE nor NapCat binaries are bundled.

Private experimental evidence is not distributed. Documentation of observed WeCom fields is not an official protocol guarantee. No open-source license grant is implied for this private repository; publishing or redistributing it should include a separate license review.
