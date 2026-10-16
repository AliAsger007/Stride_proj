# Stride Pro: coaching-studio website with user + admin modules
Run: pip install -r requirements.txt && python app.py   (http://127.0.0.1:5000)
Default admin: admin@stride.local / admin123 (set ADMIN_EMAIL, ADMIN_PASSWORD, SECRET_KEY before deploying).
Prod: gunicorn app:app --workers 2 --bind 0.0.0.0:8000 (HTTPS + persistent disk for the SQLite file).
Stripe webhook: /stripe/webhook (checkout.session.completed, checkout.session.expired).
