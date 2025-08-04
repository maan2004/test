from flask_login import UserMixin
from app import db
from datetime import datetime
import json

#----------------------------------------------------------------------------#
# Models with Enhanced State Management
#----------------------------------------------------------------------------#

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

class Designation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False, unique=True)
    hierarchy_level = db.Column(db.Integer, nullable=False)
    monthly_leave_allowance = db.Column(db.Integer, default=0)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    designation_id = db.Column(db.Integer, db.ForeignKey('designation.id'))
    designation = db.relationship('Designation', backref='employees')
    leave_dates = db.Column(db.String(255))
    gender = db.Column(db.String(10))
    shift_preference = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)  # NEW: Handle staff departures

class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    shift_template = db.Column(db.String(50))
    people_per_shift = db.Column(db.Integer)

class TeamMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'))
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'))
    team = db.relationship('Team', backref='members')
    employee = db.relationship('Employee', backref='teams')

class SavedSchedule(db.Model):
    """Stores a complete, generated monthly schedule for a team."""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False, unique=True)
    schedule_data = db.Column(db.Text, nullable=False) 
    generated_on = db.Column(db.DateTime, default=datetime.utcnow)
    team = db.relationship('Team', backref=db.backref('saved_schedule', uselist=False))

# NEW: Historical State Management Tables
class EmployeeHistory(db.Model):
    """Tracks employee assignment history for the last 3 months"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    month_year = db.Column(db.String(20), nullable=False)  # Format: "2025-01"
    shift_assigned = db.Column(db.String(50))  # Morning, Afternoon, etc.
    was_floater = db.Column(db.Boolean, default=False)
    floater_for_shift = db.Column(db.String(50))  # If floater, which shift they backed up
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    employee = db.relationship('Employee', backref='history')
    team = db.relationship('Team', backref='history')
    
    __table_args__ = (db.UniqueConstraint('employee_id', 'team_id', 'month_year'),)

class ScheduleValidationLog(db.Model):
    """Logs validation results for schedules"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    validation_result = db.Column(db.Text)  # JSON string of validation results
    violations_found = db.Column(db.Integer, default=0)
    is_valid = db.Column(db.Boolean, default=True)
    validated_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    team = db.relationship('Team', backref='validation_logs')

class APIUsageLog(db.Model):
    """Track API usage for cost control and rate limiting"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'))
    api_type = db.Column(db.String(50))  # 'generate', 'validate', 'fix'
    tokens_used = db.Column(db.Integer, default=0)
    cost_estimate = db.Column(db.Float, default=0.0)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    success = db.Column(db.Boolean, default=True)
    
    user = db.relationship('User', backref='api_usage')
    team = db.relationship('Team', backref='api_usage')

class ScheduleCache(db.Model):
    """Cache frequently used schedules and prompts"""
    id = db.Column(db.Integer, primary_key=True)
    cache_key = db.Column(db.String(255), unique=True, nullable=False)  # Hash of team config + rules
    cached_data = db.Column(db.Text, nullable=False)  # JSON data
    cache_type = db.Column(db.String(50))  # 'schedule', 'prompt', 'validation'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    hit_count = db.Column(db.Integer, default=0)

class RuleViolation(db.Model):
    """Track specific rule violations for analysis"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    rule_number = db.Column(db.Integer, nullable=False)  # Which rule was violated
    rule_description = db.Column(db.String(255))
    violation_detail = db.Column(db.Text)  # Specific details of the violation
    employee_affected = db.Column(db.String(150))
    month_year = db.Column(db.String(20))
    resolved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    team = db.relationship('Team', backref='rule_violations')
