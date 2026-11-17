---
id: rb-restore-deleted-object
type: runbook
title: "Restore a deleted file (object) from storage"
tags: storage, restore, deleted, recover, versioning, backup, undelete
---
Deleted objects can be recovered only if versioning was enabled on the bucket before deletion.

Steps:
1. Open Storage > (bucket) and turn on "Show versions".
2. Find the object; the deletion appears as a "delete marker".
3. Delete the delete marker. The previous version becomes current again.
Without versioning, deleted objects cannot be recovered. Enable versioning under bucket Settings.
