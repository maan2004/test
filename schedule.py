import random
import json
import os
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict
from flask import flash
import google.generativeai as genai
from app import db

# Import the new models for state management
from models import EmployeeHistory, ScheduleValidationLog, APIUsageLog, ScheduleCache, RuleViolation

# Enhanced scheduling rules with explicit constraints
SCHEDULING_RULES_TEXT = """
STRICT SCHEDULING RULES (ALL MUST BE ENFORCED):

1. TIERED STABILITY RULE:
   - Top hierarchy (Level 1): 3 months stability on same shift
   - Second hierarchy (Level 2): 2 months stability on same shift  
   - All lower hierarchies: MUST rotate shifts every month (NO exceptions)
   
2. FLOATER EXEMPTION RULE:
   - Highest hierarchy employees CANNOT be assigned as floaters
   - Only Level 2 and below can be floaters

3. FAIR FLOATER ROTATION RULE:
   - NO employee can be floater for 2 consecutive months
   - If employee was floater last month, they MUST be in fixed staff this month
   
4. GUARANTEED SHIFT ROTATION RULE:
   - Employees without stability perks MUST get different shift than previous month
   - Example: If intern had Morning shift in January, must get Afternoon/Evening/Night in February

5. MIXED-HIERARCHY TEAM COMPOSITION:
   - Each shift team must contain mix of hierarchy levels
   - No shift can have all same-level employees

6. FALLBACK BEHAVIORS:
   - If team understaffed: Promote floaters to fixed positions first
   - If employee on long leave: Redistribute their role to available staff
   - If sudden departure: Use most senior available floater as replacement

VALIDATION REQUIREMENTS:
- Every assignment must be checked against employee history
- Any rule violation must be flagged immediately
- Provide specific employee names and months in violation reports
"""

class EnhancedScheduleValidator:
    """Comprehensive validation engine for schedule rules"""
    
    def __init__(self, team_id):
        self.team_id = team_id
        self.violations = []
        
    def validate_against_history(self, schedule_data):
        """Validate schedule against historical assignments"""
        violations = []
        
        # Parse the schedule
        schedule = json.loads(schedule_data) if isinstance(schedule_data, str) else schedule_data
        
        for month_name, shifts in schedule.items():
            month_key = self._parse_month_key(month_name)
            
            # Check each shift assignment
            for shift_name, shift_data in shifts.items():
                # Validate fixed staff
                for staff in shift_data.get('assigned_staff', []):
                    employee_name = staff['name']
                    violations.extend(self._check_employee_rules(employee_name, shift_name, month_key, False))
                
                # Validate floaters
                for floater in shift_data.get('floaters', []):
                    employee_name = floater['name']
                    violations.extend(self._check_employee_rules(employee_name, shift_name, month_key, True))
        
        return violations
    
    def _check_employee_rules(self, employee_name, shift_name, month_key, is_floater):
        """Check all rules for a specific employee assignment"""
        violations = []
        
        # Get employee history
        from models import Employee
        employee = Employee.query.filter_by(name=employee_name).first()
        if not employee:
            return [f"Employee {employee_name} not found in database"]
        
        history = EmployeeHistory.query.filter_by(
            employee_id=employee.id, 
            team_id=self.team_id
        ).order_by(EmployeeHistory.created_at.desc()).limit(3).all()
        
        # Rule 2: Check floater exemption
        if is_floater and employee.designation.hierarchy_level == 1:
            violations.append(f"RULE 2 VIOLATION: {employee_name} (top hierarchy) cannot be assigned as floater in {month_key}")
        
        # Rule 3: Check consecutive floater assignment
        if is_floater and history:
            last_month = history[0]
            if last_month.was_floater:
                violations.append(f"RULE 3 VIOLATION: {employee_name} was floater last month, cannot be floater again in {month_key}")
        
        # Rule 4: Check shift rotation for non-stable employees
        if not is_floater and history:
            stability_months = self._get_stability_months(employee.designation.hierarchy_level)
            if stability_months <= 1:  # Must rotate monthly
                last_assignment = history[0]
                if last_assignment.shift_assigned == shift_name:
                    violations.append(f"RULE 4 VIOLATION: {employee_name} had {shift_name} shift last month, must rotate in {month_key}")
        
        return violations
    
    def _get_stability_months(self, hierarchy_level):
        """Get stability months based on hierarchy level"""
        if hierarchy_level == 1:
            return 3
        elif hierarchy_level == 2:
            return 2
        else:
            return 1
    
    def _parse_month_key(self, month_name):
        """Convert 'January 2025' to '2025-01' format"""
        try:
            date_obj = datetime.strptime(month_name, '%B %Y')
            return date_obj.strftime('%Y-%m')
        except:
            return month_name

class StateManager:
    """Manages historical state and provides context for scheduling decisions"""
    
    def __init__(self, team_id):
        self.team_id = team_id
    
    def get_employee_context(self, employee_id, months_back=3):
        """Get historical context for an employee"""
        history = EmployeeHistory.query.filter_by(
            employee_id=employee_id,
            team_id=self.team_id
        ).order_by(EmployeeHistory.created_at.desc()).limit(months_back).all()
        
        return {
            'last_shifts': [h.shift_assigned for h in history if h.shift_assigned],
            'floater_history': [h.was_floater for h in history],
            'months_since_floater': self._calculate_months_since_floater(history),
            'consecutive_shift_count': self._calculate_consecutive_shifts(history)
        }
    
    def _calculate_months_since_floater(self, history):
        """Calculate how many months since employee was a floater"""
        for i, record in enumerate(history):
            if record.was_floater:
                return i
        return len(history)  # Never been floater
    
    def _calculate_consecutive_shifts(self, history):
        """Calculate consecutive months on same shift"""
        if not history:
            return 0
        
        current_shift = history[0].shift_assigned
        count = 1
        
        for record in history[1:]:
            if record.shift_assigned == current_shift:
                count += 1
            else:
                break
        
        return count
    
    def save_assignment_history(self, schedule_data):
        """Save current schedule to history for future reference"""
        schedule = json.loads(schedule_data) if isinstance(schedule_data, str) else schedule_data
        
        for month_name, shifts in schedule.items():
            month_key = self._parse_month_key(month_name)
            
            for shift_name, shift_data in shifts.items():
                # Save fixed staff assignments
                for staff in shift_data.get('assigned_staff', []):
                    self._save_employee_history(staff['name'], month_key, shift_name, False, None)
                
                # Save floater assignments
                for floater in shift_data.get('floaters', []):
                    self._save_employee_history(floater['name'], month_key, None, True, shift_name)
    
    def _save_employee_history(self, employee_name, month_key, shift_assigned, was_floater, floater_for_shift):
        """Save individual employee history record"""
        from models import Employee
        employee = Employee.query.filter_by(name=employee_name).first()
        if not employee:
            return
        
        # Check if record already exists
        existing = EmployeeHistory.query.filter_by(
            employee_id=employee.id,
            team_id=self.team_id,
            month_year=month_key
        ).first()
        
        if existing:
            # Update existing record
            existing.shift_assigned = shift_assigned
            existing.was_floater = was_floater
            existing.floater_for_shift = floater_for_shift
        else:
            # Create new record
            history = EmployeeHistory(
                employee_id=employee.id,
                team_id=self.team_id,
                month_year=month_key,
                shift_assigned=shift_assigned,
                was_floater=was_floater,
                floater_for_shift=floater_for_shift
            )
            db.session.add(history)
        
        db.session.commit()
    
    def _parse_month_key(self, month_name):
        """Convert 'January 2025' to '2025-01' format"""
        try:
            date_obj = datetime.strptime(month_name, '%B %Y')
            return date_obj.strftime('%Y-%m')
        except:
            return month_name

class CacheManager:
    """Manages caching for schedules and prompts to reduce API costs"""
    
    @staticmethod
    def generate_cache_key(team_id, months, team_config):
        """Generate unique cache key for team configuration"""
        config_str = f"{team_id}_{months}_{json.dumps(team_config, sort_keys=True)}"
        return hashlib.md5(config_str.encode()).hexdigest()
    
    @staticmethod
    def get_cached_schedule(cache_key):
        """Retrieve cached schedule if available"""
        cache_entry = ScheduleCache.query.filter_by(
            cache_key=cache_key,
            cache_type='schedule'
        ).first()
        
        if cache_entry and cache_entry.expires_at > datetime.utcnow():
            cache_entry.hit_count += 1
            db.session.commit()
            return json.loads(cache_entry.cached_data)
        
        return None
    
    @staticmethod
    def save_to_cache(cache_key, data, cache_type='schedule', expire_hours=24):
        """Save data to cache"""
        expire_time = datetime.utcnow() + timedelta(hours=expire_hours)
        
        cache_entry = ScheduleCache(
            cache_key=cache_key,
            cached_data=json.dumps(data),
            cache_type=cache_type,
            expires_at=expire_time
        )
        
        db.session.add(cache_entry)
        db.session.commit()

def generate_monthly_assignments_enhanced(team, months, user_id=None):
    """Enhanced schedule generation with state management"""
    
    # Initialize state management
    state_manager = StateManager(team.id)
    
    # Check for cached result first
    team_config = {
        'member_count': len([m for m in team.members if m.employee.is_active]),
        'shift_template': team.shift_template,
        'people_per_shift': team.people_per_shift
    }
    
    cache_key = CacheManager.generate_cache_key(team.id, months, team_config)
    cached_schedule = CacheManager.get_cached_schedule(cache_key)
    
    if cached_schedule:
        flash("Using cached schedule to save costs.", "info")
        return cached_schedule
    
    # Get active employees only
    all_employees = sorted(
        [m.employee for m in team.members if m.employee.is_active],
        key=lambda e: e.designation.hierarchy_level
    )
    
    if not all_employees:
        flash("No active employees in this team.", "danger")
        return {}
    
    # Enhanced scheduling logic with historical context
    schedule_result = _generate_with_constraints(team, all_employees, months, state_manager)
    
    if schedule_result:
        # Save to history
        state_manager.save_assignment_history(schedule_result)
        
        # Cache the result
        CacheManager.save_to_cache(cache_key, schedule_result)
        
        # Log API usage if applicable
        if user_id:
            _log_api_usage(user_id, team.id, 'generate', len(str(schedule_result)))
    
    return schedule_result

def _generate_with_constraints(team, all_employees, months, state_manager):
    """Generate schedule with strict constraint checking"""
    
    # Configuration
    SHIFT_DESIRABILITY_ORDER = ['Morning', 'Afternoon', 'Evening', 'Night', 'Early Morning']
    
    team_shifts_map = {
        '3-shift': ['Morning', 'Afternoon', 'Night'], 
        '4-shift': ['Morning', 'Afternoon', 'Evening', 'Night'],
        '5-shift': ['Early Morning', 'Morning', 'Afternoon', 'Evening', 'Night']
    }
    
    shifts_in_template = team_shifts_map.get(team.shift_template, [])
    desirable_shifts = [s for s in SHIFT_DESIRABILITY_ORDER if s in shifts_in_template]
    num_shifts = len(desirable_shifts)
    
    if num_shifts == 0:
        flash(f"Team '{team.name}' has invalid shift template.", "danger")
        return {}
    
    people_per_shift = team.people_per_shift
    required_for_fixed = num_shifts * people_per_shift
    
    # Get historical context for all employees
    employee_contexts = {}
    for emp in all_employees:
        employee_contexts[emp.id] = state_manager.get_employee_context(emp.id)
    
    all_months_assignments = {}
    today = datetime.today()
    start_date = today.replace(day=1)
    
    # Generate for each month with constraint checking
    for month_index in range(months):
        current_year = start_date.year + (start_date.month + month_index - 1) // 12
        current_month = (start_date.month + month_index - 1) % 12 + 1
        month_name = datetime(current_year, current_month, 1).strftime('%B %Y')
        
        month_assignment = _generate_month_with_constraints(
            all_employees, employee_contexts, desirable_shifts, 
            people_per_shift, required_for_fixed, month_index
        )
        
        if month_assignment:
            all_months_assignments[month_name] = month_assignment
        else:
            flash(f"Failed to generate valid assignment for {month_name}", "danger")
            return {}
    
    return all_months_assignments

def _generate_month_with_constraints(employees, contexts, shifts, people_per_shift, required_fixed, month_index):
    """Generate single month assignment with strict constraint validation"""
    
    # Determine floaters based on constraints
    num_floaters = len(employees) - required_fixed
    floater_candidates = []
    
    if num_floaters > 0:
        # Rule 2: Top hierarchy cannot be floaters
        eligible_for_floater = [e for e in employees if e.designation.hierarchy_level > 1]
        
        # Rule 3: Prioritize those who haven't been floaters recently
        floater_candidates = sorted(eligible_for_floater, 
            key=lambda e: (-contexts[e.id]['months_since_floater'], e.designation.hierarchy_level))
        
        floater_candidates = floater_candidates[:num_floaters]
    
    # Fixed staff pool
    fixed_staff = [e for e in employees if e not in floater_candidates]
    
    # Assign shifts with rotation constraints
    monthly_assignments = {}
    available_shifts = list(shifts)
    
    # Group fixed staff and assign shifts
    shift_teams = [[] for _ in range(len(shifts))]
    for i, emp in enumerate(fixed_staff):
        shift_teams[i % len(shifts)].append(emp)
    
    # Apply rotation rules
    for i, (shift_name, team) in enumerate(zip(shifts, shift_teams)):
        # Check if any team member violates rotation rules
        for emp in team:
            context = contexts[emp.id]
            stability_months = _get_stability_months(emp.designation.hierarchy_level)
            
            if stability_months <= 1 and context['last_shifts']:  # Must rotate
                if context['last_shifts'][0] == shift_name:
                    # Try to swap with another team
                    if i + 1 < len(shift_teams):
                        shift_teams[i], shift_teams[i + 1] = shift_teams[i + 1], shift_teams[i]
        
        monthly_assignments[shift_name] = {
            'assigned_staff': [{'name': emp.name, 'designation': emp.designation.title} for emp in team],
            'floaters': []
        }
    
    # Distribute floaters across shifts
    for i, floater in enumerate(floater_candidates):
        shift_name = shifts[i % len(shifts)]
        monthly_assignments[shift_name]['floaters'].append({
            'name': floater.name, 
            'designation': floater.designation.title
        })
    
    return monthly_assignments

def _get_stability_months(hierarchy_level):
    """Get stability months based on hierarchy"""
    if hierarchy_level == 1:
        return 3
    elif hierarchy_level == 2:
        return 2
    else:
        return 1

def validate_schedule_with_enhanced_ai(schedule_data, team_id, api_key):
    """Enhanced validation with historical context"""
    
    # First, validate against historical rules
    validator = EnhancedScheduleValidator(team_id)
    rule_violations = validator.validate_against_history(schedule_data)
    
    # Then validate with AI
    enhanced_prompt = f"""
    You are an expert schedule validator. Validate this schedule against the rules and provide detailed violation reports.
    
    RULES TO CHECK:
    {SCHEDULING_RULES_TEXT}
    
    SCHEDULE TO VALIDATE:
    {schedule_data}
    
    HISTORICAL VIOLATIONS FOUND:
    {json.dumps(rule_violations)}
    
    Return a JSON object with:
    {{
        "is_valid": boolean,
        "total_violations": number,
        "violations": [list of specific violations with employee names and months],
        "severity": "low/medium/high",
        "recommendations": [list of fix suggestions]
    }}
    """
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        generation_config = genai.types.GenerationConfig(
            response_mime_type="application/json"
        )
        response = model.generate_content(enhanced_prompt, generation_config=generation_config)
        result = json.loads(response.text)
        
        # Log validation results
        _log_validation_result(team_id, result)
        
        # Save specific violations to database
        _save_rule_violations(team_id, result.get('violations', []))
        
        return result
        
    except Exception as e:
        error_result = {
            "is_valid": False,
            "total_violations": len(rule_violations),
            "violations": rule_violations + [f"AI validation error: {str(e)}"],
            "severity": "high",
            "recommendations": ["Manual review required due to AI validation failure"]
        }
        _log_validation_result(team_id, error_result)
        return error_result

def _log_api_usage(user_id, team_id, api_type, tokens_estimate):
    """Log API usage for cost tracking"""
    # Rough cost estimation (adjust based on actual API pricing)
    cost_per_1k_tokens = 0.002  # Example rate
    estimated_cost = (tokens_estimate / 1000) * cost_per_1k_tokens
    
    usage_log = APIUsageLog(
        user_id=user_id,
        team_id=team_id,
        api_type=api_type,
        tokens_used=tokens_estimate,
        cost_estimate=estimated_cost
    )
    
    db.session.add(usage_log)
    db.session.commit()

def _log_validation_result(team_id, validation_result):
    """Log validation results"""
    log_entry = ScheduleValidationLog(
        team_id=team_id,
        validation_result=json.dumps(validation_result),
        violations_found=validation_result.get('total_violations', 0),
        is_valid=validation_result.get('is_valid', False)
    )
    
    db.session.add(log_entry)
    db.session.commit()

def _save_rule_violations(team_id, violations):
    """Save individual rule violations for tracking"""
    for violation in violations:
        if isinstance(violation, str) and 'RULE' in violation:
            # Parse rule number and details
            parts = violation.split(':', 1)
            if len(parts) == 2:
                rule_part = parts[0]
                detail_part = parts[1].strip()
                
                # Extract rule number
                rule_number = None
                if 'RULE' in rule_part:
                    try:
                        rule_number = int(rule_part.split('RULE')[1].split()[0])
                    except:
                        rule_number = 0
                
                violation_record = RuleViolation(
                    team_id=team_id,
                    rule_number=rule_number or 0,
                    rule_description=rule_part,
                    violation_detail=detail_part,
                    month_year=datetime.now().strftime('%Y-%m')
                )
                
                db.session.add(violation_record)
    
    db.session.commit()
