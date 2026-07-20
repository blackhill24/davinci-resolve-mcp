/** project_db rename_folder — functional roundtrip on a temp Sm2MpFolder DB (issue #23,
 * 3.3.3). The LOSSLESS offline folder rename: a direct Sm2MpFolder.Name UPDATE on a CLOSED
 * project, versus the lossy live delete-recreate fallback in the Python server. */

import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { projectDbTool } from '../server/tools/project_db.mjs';

const require = createRequire(import.meta.url);
let Database;
try {
  Database = require('better-sqlite3');
} catch {
  Database = null;
}

test('rename_folder renames a folder in place and verifies (offline, lossless)', { skip: Database ? false : 'better-sqlite3 not installed' }, async () => {
  const dbPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pdb-rename-')), 'Project.db');
  const db = new Database(dbPath);
  db.exec(`CREATE TABLE "Sm2MpFolder" (
    "Sm2MpFolder_id" text NOT NULL,
    "DbType" text NOT NULL,
    "Name" text,
    "ColorTag" text DEFAULT 'FOLDER_COLOR_NONE'
  )`);
  db.prepare('INSERT INTO Sm2MpFolder (Sm2MpFolder_id, DbType, Name) VALUES (?,?,?)')
    .run('11111111-1111-1111-1111-111111111111', 'Sm2MpFolder', 'Renders');
  db.close();

  const res = await projectDbTool.handler({
    action: 'rename_folder',
    args: { projectDb: dbPath, folder: 'Renders', newName: 'Dailies', iConfirmProjectClosed: true },
  });
  assert.equal(res.verified, true, 'reports verified');
  assert.equal(res.from, 'Renders');
  assert.equal(res.to, 'Dailies');
  assert.ok(res.backup, 'wrote a backup');

  const check = new Database(dbPath, { readonly: true });
  const names = check.prepare('SELECT Name FROM Sm2MpFolder').all().map((r) => r.Name);
  check.close();
  assert.deepEqual(names, ['Dailies'], 'name changed on disk, no row duplicated');
});
