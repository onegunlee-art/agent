# V0.5 known limitations

- Each customer now has an independent private Git repository. Deleting that
  repository removes its managed Git object database, so another managed
  customer repository cannot recover the deleted customer's blobs. This closes
  the former shared-repository sparse-checkout limitation.
- Sparse checkout still limits the normal working-tree view only. It is not an
  operating-system sandbox and does not protect files outside the managed
  customer repository from a process with broader host permissions.
- `lines/chatbot`, `src/company_os`, and `tests` are copied into a customer
  repository at registration time. They do not update automatically when the
  public OS changes; updating them requires a separately reviewed operation.
- The deletion certificate covers the managed customer repository, its embedded
  Git history, and managed backups below the configured private registry. It
  cannot attest to copies made outside those locations.
- Private repositories and backups are not encrypted by AI Company OS. Host disk
  encryption, access control, and off-device backup policy remain operator
  responsibilities.
- Customer execution remains local and single-user. Remote and unattended
  multi-tenant operation is outside the V0.5 threat model.
