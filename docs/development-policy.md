# Pre-v2 development and compatibility policy

Status: accepted while Media Engine has no external production consumers.

## Compatibility boundary

The current mainline is a pre-stable v2 development line. API shapes, database schemas, pipeline contracts, deployment files, and internal module boundaries may change incompatibly when that produces a simpler and stronger v2 design.

The legacy `/jobs` API is temporary migration scaffolding, not a compatibility promise. Before migrating an existing deployment, tag its last known working release. Products such as whY-Tee-WebDL should then move deliberately to documented v2 contracts. Remove the legacy implementation once the required migration contract is verified; do not preserve duplicate execution paths without a current consumer.

Breaking database changes must still be represented by migrations so clean installations and development upgrades remain reproducible. Destructive data migration is acceptable before v2 stability when the change and recovery expectations are documented.

## Dependency policy

New work targets current stable language runtimes, databases, libraries, container images, API conventions, and file formats. Pin production-critical versions deliberately and verify them against current upstream documentation. Do not select an older major merely because it is familiar.

An older dependency is acceptable only for a concrete compatibility constraint. Record the exception next to the deployment or package that needs it, keep its scope narrow, and retain a test for that variant. The RK1 worker's Ubuntu 24.04 base is one such exception because its Rockchip multimedia packages are not published for Ubuntu 26.04.

Dependency updates should be automated before the first stable v2 release, with tests and image builds required before merging an update.

## Stable v2 threshold

The project will begin compatibility guarantees only after the public v2 contract is explicitly declared stable. That requires at least:

- authenticated and documented public endpoints;
- a committed OpenAPI specification and stable error model;
- versioned pipeline and artifact schemas;
- clean-install and upgrade migration tests;
- CPU and supported hardware deployment verification;
- a documented deprecation and semantic-versioning policy.

Until then, implemented endpoints are usable for development and controlled migrations but remain subject to documented breaking changes.
