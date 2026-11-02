- When the facts list a database, it belongs to this run only, with the merge base's schema already
  migrated. Head and base probes share it. If the PR adds migrations, make every base probe that
  uses the database first, then run the head's `make migrate` once and say so in the log, and make
  no base probe after that. Never drop or recreate the database, and use no other database or port.
- Run tests from the service's directory, such as `cd <run directory>/head/services/orders && make test`.
