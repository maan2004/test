from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Initialize extensions globally, without an app
db = SQLAlchemy()
login_manager = LoginManager()

def create_app():
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    
    # --- Configuration ---
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'super-secret')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'mysql+pymysql://root:root@localhost/shift_rota')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # --- Initialize Extensions with App ---
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'main.login' # Use the blueprint name

    with app.app_context():
        # Import models so SQLAlchemy knows about them
        from models import User

        # Import and register the blueprint from routes.py
        from routes import main_bp
        app.register_blueprint(main_bp)

        @login_manager.user_loader
        def load_user(user_id):
            return User.query.get(int(user_id))

        # Add a command to initialize the database
        @app.cli.command("initdb")
        def initdb_command():
            """Creates the database tables."""
            db.create_all()
            print("✅ Database tables created.")

        return app

# This is the entry point for running the application
if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)
