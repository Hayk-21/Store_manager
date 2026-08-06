"""Every SQL statement in the project lives under this package.

Modules here hold no business logic: they take arguments, run one query, and
return rows. Anything that decides *whether* a write should happen belongs in
``app.services``.
"""
