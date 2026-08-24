"""JSON API blueprints.

    app/api/meta.py     GET  /api/config, GET /health
    app/api/auth.py     admin unlock / lock / change-password
    app/api/assets.py   the /api/assets/* surface
    app/api/files.py    /api/uploads, /api/assets/<id>/files, downloads
    app/api/updates.py  /api/update-requests/*
    app/api/admin.py    /api/admin/*

Each module defines a ``bp`` that ``app/__init__.py`` registers.
"""
