from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from marshmallow import Schema, fields, validate, ValidationError
from sqlalchemy.orm import aliased
from datetime import datetime, timedelta
import json
import os
from collections import defaultdict

# Import db object and models
from app import db
from models import (User, Designation, Employee, Team, TeamMember, SavedSchedule,
                   EmployeeHistory, ScheduleValidationLog, APIUsageLog, RuleViolation)

# Import enhanced scheduler
from scheduler import (generate_monthly_assignments_enhanced, validate_schedule_with_enhanced_ai, 
                      fix_schedule_with_ai, SCHEDULING_RULES_TEXT)

main_bp = Blueprint('main', __name__)

# Rate limiting configuration
RATE_LIMITS = {
    'schedule_generation': {'limit': 10, 'window': 3600},  # 10 generations per hour
    'ai_validation': {'limit': 20, 'window': 3600},        # 20 validations per hour
    'schedule_fixes': {'limit': 5, 'window': 3600}         # 5 fixes per hour
}

# Cost control thresholds
COST_THRESHOLDS = {
    'daily_limit': 10.0,    # $10 per day per user
    'monthly_limit': 100.0,  # $100 per month per user
    'warning_threshold': 0.8  # Warn at 80% of limit
}

def check_rate_limit(user_id, action_type):
    """Check if user has exceeded rate limits"""
    if action_type not in RATE_LIMITS:
        return True, "Unknown action type"
    
    limit_config = RATE_LIMITS[action_type]
    window_start = datetime.utcnow() - timedelta(seconds=limit_config['window'])
    
    # Count recent API calls
    recent_calls = APIUsageLog.query.filter(
        APIUsageLog.user_id == user_id,
        APIUsageLog.api_type == action_type,
        APIUsageLog.timestamp >= window_start
    ).count()
    
    if recent_calls >= limit_config['limit']:
        return False, f"Rate limit exceeded. Maximum {limit_config['limit']} {action_type} calls per hour."
    
    return True, f"{recent_calls}/{limit_config['limit']} calls used in current window"

def check_cost_limits(user_id):
    """Check if user has exceeded cost thresholds"""
    now = datetime.utcnow()
    
    # Check daily costs
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_cost = db.session.query(db.func.sum(APIUsageLog.cost_estimate)).filter(
        APIUsageLog.user_id == user_id,
        APIUsageLog.timestamp >= day_start
    ).scalar() or 0.0
    
    # Check monthly costs
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_cost = db.session.query(db.func.sum(APIUsageLog.cost_estimate)).filter(
        APIUsageLog.user_id == user_id,
        APIUsageLog.timestamp >= month_start
    ).scalar() or 0.0
    
    # Check limits
    if daily_cost >= COST_THRESHOLDS['daily_limit']:
        return False, f"Daily cost limit exceeded (${daily_cost:.2f}/${COST_THRESHOLDS['daily_limit']:.2f})"
    
    if monthly_cost >= COST_THRESHOLDS['monthly_limit']:
        return False, f"Monthly cost limit exceeded (${monthly_cost:.2f}/${COST_THRESHOLDS['monthly_limit']:.2f})"
    
    # Warning thresholds
    warnings = []
    if daily_cost >= COST_THRESHOLDS['daily_limit'] * COST_THRESHOLDS['warning_threshold']:
        warnings.append(f"Daily cost warning: ${daily_cost:.2f}/${COST_THRESHOLDS['daily_limit']:.2f}")
    
    if monthly_cost >= COST_THRESHOLDS['monthly_limit'] * COST_THRESHOLDS['warning_threshold']:
        warnings.append(f"Monthly cost warning: ${monthly_cost:.2f}/${COST_THRESHOLDS['monthly_limit']:.2f}")
    
    return True, warnings

#----------------------------------------------------------------------------#
# User Authentication Routes (unchanged from original)
#----------------------------------------------------------------------------#
class UserSchema(Schema):
    username = fields.Str(required=True, validate=validate.Length(min=3, error="Username must be at least 3 characters."))
    email = fields.Email(required=True, error_messages={
        "required": "Email is required.",
        "invalid": "Please enter a valid email ID."
    })
    password = fields.Str(required=True, load_only=True)

user_schema = UserSchema()

@main_bp.route('/')
def home():
    return render_template('home.html')

@main_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        data = {
            'username': request.form['username'],
            'email': request.form['email'],
            'password': request.form['password']
        }

        try:
            validated = user_schema.load(data)
        except ValidationError as err:
            for msg in err.messages.values():
                flash(msg[0], 'danger')
            return redirect(url_for('main.signup'))

        if User.query.filter_by(email=validated['email']).first():
            flash('This email ID is already registered.', 'danger')
            return redirect(url_for('main.signup'))

        if User.query.filter_by(username=validated['username']).first():
            flash('This username is already taken.', 'danger')
            return redirect(url_for('main.signup'))

        hashed_pw = generate_password_hash(validated['password'])
        user = User(username=validated['username'], email=validated['email'], password=hashed_pw)
        db.session.add(user)
        db.session.commit()
        flash('Signup successful! You can now login.', 'success')
        return redirect(url_for('main.login'))

    return render_template('signup.html')

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = request.form['identifier']
        password = request.form['password']
        user = User.query.filter((User.email == identifier) | (User.username == identifier)).first()

        if not user:
            flash('User not found. Please check your email or username.', 'danger')
            return redirect(url_for('main.login'))

        if not check_password_hash(user.password, password):
            flash('Incorrect password.', 'danger')
            return redirect(url_for('main.login'))

        login_user(user)
        return redirect(url_for('main.dashboard'))

    return render_template('login.html')

@main_bp.route('/dashboard')
@login_required
def dashboard():
    # Show cost usage summary on dashboard
    user_id = current_user.id
    now = datetime.utcnow()
    
    # Get current usage stats
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    daily_cost = db.session.query(db.func.sum(APIUsageLog.cost_estimate)).filter(
        APIUsageLog.user_id == user_id,
        APIUsageLog.timestamp >= day_start
    ).scalar() or 0.0
    
    monthly_cost = db.session.query(db.func.sum(APIUsageLog.cost_estimate)).filter(
        APIUsageLog.user_id == user_id,
        APIUsageLog.timestamp >= month_start
    ).scalar() or 0.0
    
    usage_stats = {
        'daily_cost': daily_cost,
        'monthly_cost': monthly_cost,
        'daily_limit': COST_THRESHOLDS['daily_limit'],
        'monthly_limit': COST_THRESHOLDS['monthly_limit']
    }
    
    return render_template('dashboard.html', user=current_user, usage_stats=usage_stats)

@main_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('main.login'))

#----------------------------------------------------------------------------#
# Management Routes (keeping existing designation and employee management)
#----------------------------------------------------------------------------#

@main_bp.route('/designation/add', methods=['GET', 'POST'])
@login_required
def add_designation():
    if request.method == 'POST':
        title = request.form['title'].strip().title()
        hierarchy = request.form['hierarchy']
        leave = request.form['leave']

        try:
            hierarchy_level = int(hierarchy)
        except ValueError:
            flash('Hierarchy must be a number.', 'danger')
            return redirect(url_for('main.add_designation'))

        if Designation.query.filter_by(title=title).first():
            flash('This designation title already exists.', 'danger')
            return redirect(url_for('main.add_designation'))

        if Designation.query.filter_by(hierarchy_level=hierarchy_level).first():
            flash(f'Hierarchy level {hierarchy_level} is already assigned.', 'danger')
            return redirect(url_for('main.add_designation'))

        designation = Designation(
            title=title,
            hierarchy_level=hierarchy_level,
            monthly_leave_allowance=int(leave)
        )
        db.session.add(designation)
        db.session.commit()
        flash('Designation added successfully.', 'success')
        return redirect(url_for('main.manage_designation'))

    return render_template('designation_add.html')

@main_bp.route('/designation/manage', methods=['GET', 'POST'])
@login_required
def manage_designation():
    designations = Designation.query.order_by(Designation.hierarchy_level).all()

    if request.method == 'POST':
        if 'delete_id' in request.form:
            delete_id = int(request.form['delete_id'])
            designation_to_delete = Designation.query.get(delete_id)
            if designation_to_delete:
                db.session.delete(designation_to_delete)
                db.session.commit()
                flash(f'Designation "{designation_to_delete.title}" deleted.', 'info')
                return redirect(url_for('main.manage_designation'))

        # Validation for bulk updates
        new_titles = []
        new_hierarchies = []
        for desig in designations:
            new_title = request.form.get(f"title_{desig.id}").strip()
            new_hierarchy = int(request.form.get(f"hierarchy_{desig.id}"))
            if new_title in new_titles:
                flash(f'Duplicate designation title "{new_title}" found.', 'danger')
                return redirect(url_for('main.manage_designation'))
            new_titles.append(new_title)
            if new_hierarchy in new_hierarchies:
                flash(f'Duplicate hierarchy level "{new_hierarchy}" found.', 'danger')
                return redirect(url_for('main.manage_designation'))
            new_hierarchies.append(new_hierarchy)

        for desig in designations:
            desig.title = request.form.get(f"title_{desig.id}").strip().title()
            desig.hierarchy_level = int(request.form.get(f"hierarchy_{desig.id}"))
            desig.monthly_leave_allowance = int(request.form.get(f"leave_{desig.id}"))

        db.session.commit()
        flash('Changes saved successfully.', 'success')
        return redirect(url_for('main.manage_designation'))

    return render_template('designation_manage.html', designations=designations)

@main_bp.route('/management')
@login_required
def management_dashboard():
    return render_template('management_dashboard.html')

@main_bp.route('/employee/dashboard')
@login_required
def employee_dashboard():
    return render_template('employee_dashboard.html')

# Employee management routes (keeping existing functionality)
@main_bp.route('/employee/add', methods=['GET', 'POST'])
@login_required
def add_employee():
    designations = Designation.query.all()
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        gender = request.form['gender']
        designation_id = int(request.form['designation_id'])
        leave_dates_raw = request.form.get('leave_dates', '')

        if Employee.query.filter_by(email=email).first():
            flash('An employee with this email already exists.', 'danger')
            return redirect(url_for('main.add_employee'))

        leave_dates_list = [d.strip() for d in leave_dates_raw.split(',') if d.strip()]
        today = datetime.today().date()
        parsed_dates = []
        for d in leave_dates_list:
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d").date()
                if parsed < today:
                    flash(f"Leave date {d} is in the past.", "danger")
                    return redirect(url_for('main.add_employee'))
                parsed_dates.append(parsed)
            except ValueError:
                flash(f"Invalid date format: {d}", "danger")
                return redirect(url_for('main.add_employee'))

        designation = Designation.query.get(designation_id)
        max_allowed = designation.monthly_leave_allowance if designation else 0
        month_count = defaultdict(int)
        for date in parsed_dates:
            key = (date.year, date.month)
            month_count[key] += 1
        for (year, month), count in month_count.items():
            if count > max_allowed:
                flash(f"Too many leaves in {year}-{month:02d}. Max allowed is {max_allowed}.", "danger")
                return redirect(url_for('main.add_employee'))

        leave_dates_json = json.dumps([d.strftime("%Y-%m-%d") for d in parsed_dates])
        employee = Employee(
            name=name,
            email=email,
            gender=gender,
            designation_id=designation_id,
            shift_preference=request.form.get('shift_preference') or None,
            leave_dates=leave_dates_json,
            is_active=True  # New field
        )
        db.session.add(employee)
        db.session.commit()
        flash('Employee added successfully!', 'success')
        return redirect(url_for('main.manage_employees'))
    return render_template('employee_add.html', designations=designations)

@main_bp.route('/employees/manage', methods=['GET', 'POST'])
@login_required
def manage_employees():
    employees = Employee.query.all()
    designations = Designation.query.all()
    if request.method == 'POST':
        emp_id = int(request.form['emp_id'])
        employee = Employee.query.get(emp_id)
        if request.form['action'] == 'delete':
            # Instead of deleting, mark as inactive
            employee.is_active = False
            db.session.commit()
            flash('Employee marked as inactive.', 'info')
            return redirect(url_for('main.manage_employees'))
        elif request.form['action'] == 'reactivate':
            employee.is_active = True
            db.session.commit()
            flash('Employee reactivated.', 'success')
            return redirect(url_for('main.manage_employees'))

        # Update employee data
        employee.name = request.form['name']
        employee.email = request.form['email']
        employee.gender = request.form['gender']
        employee.designation_id = int(request.form['designation_id'])
        employee.shift_preference = request.form.get('shift_preference') or None
        raw_dates = request.form.get('leave_dates', '')
        leave_list = [d.strip() for d in raw_dates.split(',') if d.strip()]
        today = datetime.today().date()
        for d in leave_list:
            parsed = datetime.strptime(d, "%Y-%m-%d").date()
            if parsed < today:
                flash('Leave date in the past: {}'.format(d), 'danger')
                return redirect(url_for('main.manage_employees'))
        month_map = {}
        for d in leave_list:
            month_key = d[:7]
            month_map[month_key] = month_map.get(month_key, 0) + 1
        max_allowed = Designation.query.get(employee.designation_id).monthly_leave_allowance
        for month, count in month_map.items():
            if count > max_allowed:
                flash(f'Maximum leaves reached in {month}. Allowed: {max_allowed}', 'danger')
                return redirect(url_for('main.manage_employees'))
        employee.leave_dates = json.dumps(leave_list)
        db.session.commit()
        flash('Changes saved successfully.', 'success')
        return redirect(url_for('main.manage_employees'))
    
    for emp in employees:
        try:
            emp.leave_dates_formatted = json.loads(emp.leave_dates or '[]')
        except:
            emp.leave_dates_formatted = []
    return render_template('employee_manage.html', employees=employees, designations=designations)

# Team management routes (keeping existing functionality)
@main_bp.route('/team/dashboard')
@login_required
def view_teams():
    teams = Team.query.all()
    return render_template('team_dashboard.html', teams=teams)

@main_bp.route('/team/add', methods=['GET', 'POST'])
@login_required
def add_team():
    TeamMemberAlias = aliased(TeamMember)
    unassigned_employees = (
        db.session.query(Employee)
        .outerjoin(TeamMemberAlias, Employee.id == TeamMemberAlias.employee_id)
        .filter(TeamMemberAlias.employee_id == None, Employee.is_active == True)  # Only active employees
        .all()
    )
    
    if request.method == 'POST':
        name = request.form['name']
        template = request.form['template']
        people = int(request.form['people'])
        member_ids = list(map(int, request.form.getlist('members')))
        shift_map = {"3-shift": 3, "4-shift": 4, "5-shift": 5}
        shift_count = shift_map.get(template, 0)
        required_min_members = shift_count * people
        
        if Team.query.filter_by(name=name).first():
            flash('A team with this name already exists.', 'danger')
            return redirect(url_for('main.add_team'))
            
        if len(member_ids) < required_min_members:
            flash(f"A minimum of {required_min_members} employees required for {template} template with {people} people/shift.", 'danger')
            return redirect(url_for('main.add_team'))
            
        selected_employees = Employee.query.filter(Employee.id.in_(member_ids)).all()
        male_count = sum(1 for e in selected_employees if e.gender == 'Male')
        female_count = sum(1 for e in selected_employees if e.gender == 'Female')
        
        if male_count < 2 or female_count < 2:
            flash('A team must include at least 2 members from each gender (minimum 2 males and 2 females).', 'danger')
            return redirect(url_for('main.add_team'))
            
        team = Team(name=name, shift_template=template, people_per_shift=people)
        db.session.add(team)
        db.session.commit()
        
        for eid in member_ids:
            db.session.add(TeamMember(team_id=team.id, employee_id=eid))
        db.session.commit()
        
        flash('Team added successfully.', 'success')
        return redirect(url_for('main.view_teams'))
        
    return render_template('team_add.html', employees=unassigned_employees)

@main_bp.route('/team/manage', methods=['GET', 'POST'])
@login_required
def manage_teams():
    teams = Team.query.all()
    team_members_map = {team.id: {m.employee_id for m in team.members if m.employee.is_active} for team in teams}
    
    if request.method == 'POST':
        if request.form['action'] == 'delete':
            team_id = int(request.form['team_id'])
            team = Team.query.get(team_id)
            if team:
                # Delete related schedules and history
                SavedSchedule.query.filter_by(team_id=team_id).delete()
                EmployeeHistory.query.filter_by(team_id=team_id).delete()
                for tm in team.members:
                    db.session.delete(tm)
                db.session.delete(team)
                db.session.commit()
                flash('Team and all associated data deleted successfully.', 'info')
            return redirect(url_for('main.manage_teams'))
            
        # Update team logic (keeping existing)
        team_id = int(request.form['team_id'])
        team = Team.query.get(team_id)
        team.name = request.form['name']
        team.shift_template = request.form['template']
        team.people_per_shift = int(request.form['people'])
        selected_ids = set(map(int, request.form.getlist('members')))
        
        if not selected_ids:
            flash('You must select at least one team member.', 'danger')
            return redirect(url_for('main.manage_teams'))
            
        shift_multiplier = {'3-shift': 3, '4-shift': 4, '5-shift': 5}.get(team.shift_template, 3)
        required_min = shift_multiplier * team.people_per_shift
        
        if len(selected_ids) < required_min:
            flash(f'You must select at least {required_min} members for {team.shift_template} with {team.people_per_shift} people per shift.', 'danger')
            return redirect(url_for('main.manage_teams'))
            
        selected_emps = Employee.query.filter(Employee.id.in_(selected_ids), Employee.is_active == True).all()
        male_count = sum(1 for e in selected_emps if e.gender == 'Male')
        female_count = sum(1 for e in selected_emps if e.gender == 'Female')
        
        if male_count < 2 and female_count < 2:
            flash('A team must include at least 2 members of the opposite gender.', 'danger')
            return redirect(url_for('main.manage_teams'))
            
        current_ids = {m.employee_id for m in team.members}
        for emp_id in selected_ids - current_ids:
            db.session.add(TeamMember(team_id=team.id, employee_id=emp_id))
        for tm in team.members[:]:
            if tm.employee_id not in selected_ids:
                db.session.delete(tm)
        db.session.commit()
        flash('Team updated successfully.', 'success')
        return redirect(url_for('main.manage_teams'))
    
    employee_map = {}
    for team in teams:
        all_assigned_ids = {m.employee_id for t in teams for m in t.members if t.id != team.id and m.employee.is_active}
        available_emps = Employee.query.filter(~Employee.id.in_(all_assigned_ids), Employee.is_active == True).all()
        employee_map[team.id] = available_emps
        
    return render_template('team_manage.html', teams=teams, employee_map=employee_map, team_members_map=team_members_map)

#----------------------------------------------------------------------------#
# Enhanced Schedule Generation Routes
#----------------------------------------------------------------------------#

@main_bp.route('/generate_schedule', methods=['GET', 'POST'])
@login_required
def generate_schedule():
    teams = Team.query.all()
    selected_team = None
    schedule_by_month = None
    schedule_exists = False
    ai_validation_report = None
    cost_warnings = []
    
    # Check cost limits
    cost_ok, cost_message = check_cost_limits(current_user.id)
    if not cost_ok:
        flash(cost_message, 'danger')
    elif isinstance(cost_message, list):
        cost_warnings = cost_message
    
    # Handle page load and team selection
    if request.method == 'GET':
        team_id = request.args.get('team_id', type=int)
        if team_id:
            selected_team = Team.query.get(team_id)
            if selected_team:
                saved_schedule = SavedSchedule.query.filter_by(team_id=team_id).first()
                if saved_schedule:
                    schedule_by_month = json.loads(saved_schedule.schedule_data)
                    schedule_exists = True
                    
                    # Enhanced validation with historical context
                    api_key = os.getenv('GEMINI_API_KEY')
                    if api_key:
                        ai_validation_report = validate_schedule_with_enhanced_ai(
                            saved_schedule.schedule_data, team_id, api_key
                        )

    # Handle new schedule generation with enhanced controls
    if request.method == 'POST':
        team_id = int(request.form['team_id'])
        months = int(request.form.get('months', 1))
        selected_team = Team.query.get(team_id)

        # Check rate limits
        rate_ok, rate_message = check_rate_limit(current_user.id, 'generate')
        if not rate_ok:
            flash(rate_message, 'danger')
            return redirect(url_for('main.generate_schedule', team_id=team_id))

        # Check cost limits
        cost_ok, cost_message = check_cost_limits(current_user.id)
        if not cost_ok:
            flash(cost_message, 'danger')
            return redirect(url_for('main.generate_schedule', team_id=team_id))

        if SavedSchedule.query.filter_by(team_id=team_id).first():
            flash("A schedule for this team already exists. Delete it first to generate a new one.", "warning")
            return redirect(url_for('main.generate_schedule', team_id=team_id))

        # Generate schedule with enhanced state management
        schedule_by_month = generate_monthly_assignments_enhanced(selected_team, months, current_user.id)

        if schedule_by_month:
            new_schedule = SavedSchedule(
                team_id=team_id,
                schedule_data=json.dumps(schedule_by_month)
            )
            db.session.add(new_schedule)
            db.session.commit()
            flash("New schedule generated and saved with enhanced validation!", "success")
            return redirect(url_for('main.generate_schedule', team_id=team_id))
        else:
            flash("Failed to generate schedule. Please check team configuration and try again.", "danger")

    return render_template(
        'generate_schedule_enhanced.html',
        teams=teams,
        selected_team=selected_team,
        schedule_by_month=schedule_by_month,
        schedule_exists=schedule_exists,
        ai_validation_report=ai_validation_report,
        cost_warnings=cost_warnings,
        months=1
    )

@main_bp.route('/fix_schedule/<int:team_id>', methods=['POST'])
@login_required
def fix_schedule(team_id):
    """Enhanced schedule fixing with rate limiting"""
    
    # Check rate limits
    rate_ok, rate_message = check_rate_limit(current_user.id, 'schedule_fixes')
    if not rate_ok:
        flash(rate_message, 'danger')
        return redirect(url_for('main.generate_schedule', team_id=team_id))
    
    # Check cost limits
    cost_ok, cost_message = check_cost_limits(current_user.id)
    if not cost_ok:
        flash(cost_message, 'danger')
        return redirect(url_for('main.generate_schedule', team_id=team_id))

    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        flash("GEMINI_API_KEY not found in environment.", "danger")
        return redirect(url_for('main.generate_schedule', team_id=team_id))

    saved_schedule = SavedSchedule.query.filter_by(team_id=team_id).first()
    if not saved_schedule:
        flash("No schedule found to fix.", "danger")
        return redirect(url_for('main.generate_schedule', team_id=team_id))

    # Re-validate with enhanced validation
    validation_report = validate_schedule_with_enhanced_ai(
        saved_schedule.schedule_data, team_id, api_key
    )
    violations = validation_report.get('violations', [])

    if not violations:
        flash("No violations found, schedule is already valid.", "info")
        return redirect(url_for('main.generate_schedule', team_id=team_id))

    # Attempt AI-powered fix
    corrected_schedule, success = fix_schedule_with_ai(
        saved_schedule.schedule_data,
        violations,
        SCHEDULING_RULES_TEXT,
        api_key
    )

    if success:
        # Validate the corrected schedule
        corrected_validation = validate_schedule_with_enhanced_ai(
            json.dumps(corrected_schedule), team_id, api_key
        )
        
        if corrected_validation.get('is_valid', False):
            saved_schedule.schedule_data = json.dumps(corrected_schedule)
            db.session.commit()
            flash("Schedule successfully corrected by AI and validated!", "success")
        else:
            flash("AI correction partially successful but still has some violations. Manual review recommended.", "warning")
    else:
        flash(f"AI correction failed: {corrected_schedule.get('error', 'Unknown error')}", "danger")

    return redirect(url_for('main.generate_schedule', team_id=team_id))

@main_bp.route('/schedule_analytics')
@login_required
def schedule_analytics():
    """New route for viewing schedule analytics and violations"""
    
    # Get user's teams
    user_teams = Team.query.all()  # In a real app, filter by user's teams
    
    analytics_data = {}
    for team in user_teams:
        # Get violation history
        violations = RuleViolation.query.filter_by(team_id=team.id).order_by(RuleViolation.created_at.desc()).limit(10).all()
        
        # Get validation logs
        validation_logs = ScheduleValidationLog.query.filter_by(team_id=team.id).order_by(ScheduleValidationLog.validated_at.desc()).limit(5).all()
        
        # Get API usage for this team
        api_usage = APIUsageLog.query.filter_by(team_id=team.id).order_by(APIUsageLog.timestamp.desc()).limit(10).all()
        
        analytics_data[team.id] = {
            'team': team,
            'violations': violations,
            'validation_logs': validation_logs,
            'api_usage': api_usage,
            'total_violations': len(violations),
            'last_validation': validation_logs[0] if validation_logs else None
        }
    
    return render_template('schedule_analytics.html', analytics_data=analytics_data)

@main_bp.route('/api_usage_report')
@login_required
def api_usage_report():
    """Generate API usage and cost report for current user"""
    
    user_id = current_user.id
    now = datetime.utcnow()
    
    # Get usage for different time periods
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    usage_summary = {}
    for period, start_date in [('today', day_start), ('week', week_start), ('month', month_start)]:
        usage = APIUsageLog.query.filter(
            APIUsageLog.user_id == user_id,
            APIUsageLog.timestamp >= start_date
        ).all()
        
        usage_summary[period] = {
            'total_calls': len(usage),
            'total_cost': sum(u.cost_estimate for u in usage),
            'total_tokens': sum(u.tokens_used for u in usage),
            'by_type': defaultdict(lambda: {'calls': 0, 'cost': 0.0, 'tokens': 0})
        }
        
        for u in usage:
            usage_summary[period]['by_type'][u.api_type]['calls'] += 1
            usage_summary[period]['by_type'][u.api_type]['cost'] += u.cost_estimate
            usage_summary[period]['by_type'][u.api_type]['tokens'] += u.tokens_used
    
    return render_template('api_usage_report.html', 
                         usage_summary=usage_summary,
                         cost_thresholds=COST_THRESHOLDS,
                         rate_limits=RATE_LIMITS)

@main_bp.route('/delete_schedule/<int:team_id>', methods=['POST'])
@login_required
def delete_schedule(team_id):
    """Enhanced schedule deletion with cleanup"""
    schedule_to_delete = SavedSchedule.query.filter_by(team_id=team_id).first()
    if schedule_to_delete:
        # Clean up related data
        ScheduleValidationLog.query.filter_by(team_id=team_id).delete()
        RuleViolation.query.filter_by(team_id=team_id).delete()
        
        db.session.delete(schedule_to_delete)
        db.session.commit()
        flash('Schedule and all related validation data deleted successfully.', 'success')
    else:
        flash('No schedule found for this team.', 'warning')
    
    return redirect(url_for('main.generate_schedule', team_id=team_id))

@main_bp.route('/batch_generate', methods=['GET', 'POST'])
@login_required
def batch_generate():
    """Generate schedules for multiple teams in batches to control costs"""
    
    if request.method == 'GET':
        teams = Team.query.all()
        return render_template('batch_generate.html', teams=teams)
    
    selected_team_ids = request.form.getlist('team_ids')
    months = int(request.form.get('months', 1))
    
    if not selected_team_ids:
        flash('Please select at least one team.', 'danger')
        return redirect(url_for('main.batch_generate'))
    
    # Check if batch generation would exceed limits
    estimated_cost = len(selected_team_ids) * months * 0.05  # Rough estimate
    
    cost_ok, cost_message = check_cost_limits(current_user.id)
    if not cost_ok:
        flash(f'Batch generation blocked: {cost_message}', 'danger')
        return redirect(url_for('main.batch_generate'))
    
    # Generate schedules in batches
    success_count = 0
    failed_teams = []
    
    for team_id in selected_team_ids:
        team = Team.query.get(int(team_id))
        if not team:
            continue
            
        # Check if schedule already exists
        if SavedSchedule.query.filter_by(team_id=team.id).first():
            failed_teams.append(f"{team.name} (schedule exists)")
            continue
        
        # Check rate limits for each generation
        rate_ok, rate_message = check_rate_limit(current_user.id, 'generate')
        if not rate_ok:
            failed_teams.append(f"{team.name} (rate limited)")
            break
        
        # Generate schedule
        schedule_result = generate_monthly_assignments_enhanced(team, months, current_user.id)
        
        if schedule_result:
            new_schedule = SavedSchedule(
                team_id=team.id,
                schedule_data=json.dumps(schedule_result)
            )
            db.session.add(new_schedule)
            success_count += 1
        else:
            failed_teams.append(f"{team.name} (generation failed)")
    
    db.session.commit()
    
    if success_count > 0:
        flash(f'Successfully generated schedules for {success_count} teams.', 'success')
    
    if failed_teams:
        flash(f'Failed for: {", ".join(failed_teams)}', 'warning')
    
    return redirect(url_for('main.batch_generate'))

@main_bp.route('/emergency_schedule/<int:team_id>')
@login_required
def emergency_schedule(team_id):
    """Generate emergency/fallback schedule when AI fails"""
    
    team = Team.query.get_or_404(team_id)
    
    # Simple round-robin fallback schedule
    active_employees = [m.employee for m in team.members if m.employee.is_active]
    
    if len(active_employees) < 3:
        flash('Not enough active employees for emergency schedule.', 'danger')
        return redirect(url_for('main.generate_schedule', team_id=team_id))
    
    # Create minimal safe schedule
    team_shifts_map = {
        '3-shift': ['Morning', 'Afternoon', 'Night'], 
        '4-shift': ['Morning', 'Afternoon', 'Evening', 'Night'],
        '5-shift': ['Early Morning', 'Morning', 'Afternoon', 'Evening', 'Night']
    }
    
    shifts = team_shifts_map.get(team.shift_template, ['Morning', 'Afternoon', 'Night'])
    people_per_shift = team.people_per_shift
    
    # Simple assignment - divide employees evenly
    emergency_schedule = {}
    today = datetime.today()
    month_name = today.strftime('%B %Y')
    
    monthly_assignment = {}
    for i, shift in enumerate(shifts):
        shift_team = []
        floaters = []
        
        # Assign fixed staff
        for j in range(people_per_shift):
            emp_index = (i * people_per_shift + j) % len(active_employees)
            shift_team.append({
                'name': active_employees[emp_index].name,
                'designation': active_employees[emp_index].designation.title
            })
        
        # Remaining employees as floaters
        remaining_start = len(shifts) * people_per_shift
        if remaining_start < len(active_employees):
            floater_index = remaining_start + i
            if floater_index < len(active_employees):
                floaters.append({
                    'name': active_employees[floater_index].name,
                    'designation': active_employees[floater_index].designation.title
                })
        
        monthly_assignment[shift] = {
            'assigned_staff': shift_team,
            'floaters': floaters
        }
    
    emergency_schedule[month_name] = monthly_assignment
    
    # Save emergency schedule
    existing_schedule = SavedSchedule.query.filter_by(team_id=team_id).first()
    if existing_schedule:
        existing_schedule.schedule_data = json.dumps(emergency_schedule)
    else:
        new_schedule = SavedSchedule(
            team_id=team_id,
            schedule_data=json.dumps(emergency_schedule)
        )
        db.session.add(new_schedule)
    
    db.session.commit()
    
    flash('Emergency schedule generated successfully. Please review and adjust as needed.', 'warning')
    return redirect(url_for('main.generate_schedule', team_id=team_id))

# Legacy route handlers (keeping for compatibility)
@main_bp.route('/employee/delete', methods=['POST'])
@login_required
def delete_employee():
    emp_id = int(request.form['emp_id'])
    emp = Employee.query.get(emp_id)
    if not emp:
        flash('Employee not found.', 'danger')
        return redirect(url_for('main.manage_employees'))
    
    # Mark as inactive instead of deleting
    emp.is_active = False
    db.session.commit()
    flash(f'Employee {emp.name} marked as inactive.', 'info')
    return redirect(url_for('main.manage_employees'))

@main_bp.route('/employee/update', methods=['POST'])
@login_required
def update_employee():
    emp_id = int(request.form['emp_id'])
    designation_id = int(request.form['designation_id'])
    leave_dates = request.form['leave_dates']
    emp = Employee.query.get(emp_id)
    if not emp:
        flash('Employee not found.', 'danger')
        return redirect(url_for('main.manage_employees'))
    emp.designation_id = designation_id
    emp.leave_dates = leave_dates
    db.session.commit()
    flash(f'Changes for {emp.name} saved successfully.', 'success')
    return redirect(url_for('main.manage_employees'))

@main_bp.route('/team/delete/<int:team_id>', methods=['POST'])
@login_required
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)
    
    # Clean up all related data
    SavedSchedule.query.filter_by(team_id=team_id).delete()
    EmployeeHistory.query.filter_by(team_id=team_id).delete()
    ScheduleValidationLog.query.filter_by(team_id=team_id).delete()
    RuleViolation.query.filter_by(team_id=team_id).delete()
    APIUsageLog.query.filter_by(team_id=team_id).delete()
    
    # Delete team members and team
    for tm in team.members:
        db.session.delete(tm)
    db.session.delete(team)
    db.session.commit()
    
    flash('Team and all associated data deleted successfully.', 'info')
    return redirect(url_for('main.manage_teams'))
