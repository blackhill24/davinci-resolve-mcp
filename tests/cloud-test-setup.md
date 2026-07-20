# Cloud project live-test setup (issue #25, epic #26 stage 5)

`tests/live_cloud_project_validation.py` live-tests the four ProjectManager
cloud methods (`CreateCloudProject`, `LoadCloudProject`, `ImportCloudProject`,
`RestoreCloudProject`) and the `MediaPoolItem` "Cloud Sync" clip-property
readback. It needs a Blackmagic Cloud account and skips cleanly (exit 0) when
one isn't configured — it never fails the offline/live suites for
contributors without cloud access.

## Requirements

- DaVinci Resolve Studio 21, signed in to a Blackmagic Cloud account (Resolve
  Preferences > General, or the cloud-project dialog in Project Manager).
- A **disposable** cloud test library / project space — the probe creates
  real cloud projects under the given media path. Don't point this at a
  production library.
- Node.js is not required for this stage.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `RESOLVE_CLOUD_PROJECT_MEDIA_PATH` | yes | Local path Resolve syncs cloud media into (`resolve.CLOUD_SETTING_PROJECT_MEDIA_PATH`). Absence is what makes the probe skip. |
| `RESOLVE_CLOUD_TEST_PREFIX` | no | Prefix for disposable project names (default `_mcp_cloud_probe`). |
| `RESOLVE_CLOUD_TEST_IMPORT_FILE` | no | Path to a cloud-project file to exercise `ImportCloudProject`. Import/restore steps are skipped (`not_applicable`) without it. |
| `RESOLVE_CLOUD_TEST_RESTORE_FOLDER` | no | Folder path to exercise `RestoreCloudProject`. |

## Run

```
RESOLVE_CLOUD_PROJECT_MEDIA_PATH=/path/to/disposable/library \
  .venv/bin/python tests/live_cloud_project_validation.py --output-dir /tmp/cloud-project-probe
```

Writes `cloud-project-probe.json` / `.md` to the output dir, same shape as
the other `live_*_validation.py` probes (see `src/utils/timeline_kernel_probe.py`).

## Cleanup — read before running

**There is no scripted way to delete a cloud project from Blackmagic Cloud
storage.** The documented API surface is only `Create/Load/Import/
RestoreCloudProject` — no `DeleteCloudProject`, no `GetCloudProjectList` (see
the `api_truth` entry "Cloud project enumeration / export / user
management"). The probe deletes the **local** project reference it creates
(`project_manager.safe_project_delete`) in its `finally` block, but any
cloud-side copy persists until you remove it manually from the Blackmagic
Cloud account after each run. Check the account's project list after every
probe run and delete the `_mcp_cloud_probe_*` disposables by hand.

## The documented Load quirk

Per `docs/reference/resolve_scripting_api.txt` lines 597-598: only the
**first** `LoadCloudProject` call on a given system honours
`project_media_path` and `sync_mode`; subsequent loads honour only
`project_name`. The probe's `cloud_load` stage calls `load` twice with
different `sync_mode` values on the same project to surface this — expect the
second call's settings to be silently ignored, not an error.
