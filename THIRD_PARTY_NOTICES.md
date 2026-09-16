# Provenance and third-party dependencies

The Python implementation and bounded codecs were migrated from the user's locally developed IM Unified CLI experiment. The selected codec code was written for that experiment; no complete Tencent client, decrypted database, credential extractor or external exporter is bundled.

The source repository depends on **ChatLab CLI 0.37.1**, licensed AGPL-3.0 by its upstream authors. Windows portable release assets additionally aggregate the unmodified CLI backend and its dependencies with Node 22.22.2; the backend remains a separately invoked process. Each asset includes Node's license, ChatLab's license and corresponding source archive at commit `d844578bab17d68f8f960dea4675781645c170ff`, Python/runtime notices, dependency metadata and shipped node_modules notices. Do not remove those materials when redistributing. The repository itself does not commit node_modules or compiled dependency binaries.

Optional Windows access uses pywin32 and uiautomation. Portable builds use PyInstaller; their supplied notices are kept in the release's licenses directory. The QCE compatibility parser consumes a reviewed export representation; neither QCE nor NapCat client binaries are bundled.

Private experimental evidence is not distributed. Documentation of observed WeCom fields is not an official protocol guarantee. No open-source license grant is implied for this private repository; publishing or redistributing it should include a separate license review.
