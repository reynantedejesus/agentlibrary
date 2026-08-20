"""JSON API blueprints.

    app/api/meta.py    GET  /api/config, GET /health
    app/api/auth.py    POST /api/auth/login|logout|change-password, GET /api/auth/me
    app/api/assets.py  the /api/assets/* surface + /api/my-submissions
    app/api/files.py   /api/assets/<id>/files, /api/files/<id>/download
    app/api/admin.py   /api/admin/*

Each module defines a ``bp`` that ``app/__init__.py`` registers.
"""
