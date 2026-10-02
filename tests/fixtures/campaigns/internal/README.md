# Internal-contract fixtures (M10.5)

`platform-lib` produces the internal package `acme-shared`; `svc-orders` and `svc-billing`
call the removed `records.fetch`; `svc-reports` only uses the unchanged `health.check`;
`acme-shared-fork` merely *sounds* related -- it has no dependency evidence and must never
be matched by name.
