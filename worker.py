"""
Background worker task for schedule generation.
This runs in a separate process via RQ worker.
"""
import os
from datetime import datetime
from generate_schedule import generate_and_save
from app import app

def generate_schedule_task(start_year, start_month, end_year, end_month, service_id):
    """
    Background task to generate schedule.
    This function is executed by the RQ worker.
    """
    try:
        # Run the schedule generation
        with app.app_context():
            generate_and_save(start_year, start_month, end_year, end_month, service_id)
        
        return {
            'status': 'completed',
            'message': f'Schedule generated successfully for {start_year}/{start_month} to {end_year}/{end_month}'
        }
    except Exception as e:
        return {
            'status': 'failed',
            'message': f'Schedule generation failed: {str(e)}'
        }
