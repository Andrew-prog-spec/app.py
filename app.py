import os
import asyncio
import pandas as pd
import threading
import sqlite3
import shutil
from flask import Flask, request, render_template_string, flash, redirect, url_for, session, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError, PhoneNumberInvalidError
from telethon.tl.types import InputPeerUser
import secrets  # For secure session naming

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', secrets.token_hex(16))

# Get credentials from environment variables (get from my.telegram.org)
API_ID_STR = os.environ.get('TELEGRAM_API_ID', '').strip()
API_HASH_STR = os.environ.get('TELEGRAM_API_HASH', '').strip()

# Store credentials globally, will be set when available
API_ID = None
API_HASH = None

# Try to set credentials if available
if API_ID_STR and API_HASH_STR:
    try:
        API_ID = int(API_ID_STR)
        API_HASH = API_HASH_STR  # Fix: Use the string from environment, not the None variable
        print(f"✅ Telegram API credentials loaded successfully: API_ID={API_ID}, API_HASH={'*' * len(API_HASH)}")
    except ValueError:
        print("Warning: TELEGRAM_API_ID must be a valid integer")
else:
    print("Warning: TELEGRAM_API_ID and TELEGRAM_API_HASH not set. App will start but Telegram features will be disabled.")

# Phone number will be provided by user

# Authentication state (no global client)
auth_state = {
    'code_requested': False,
    'is_authenticated': False,
    'phone_code_hash': None,
    'phone_number': None,
    'session_string': None,  # Temporary StringSession during auth flow
    'monitoring_session_string': None  # Final StringSession for monitoring to avoid DB conflicts
}

# Global state for message sending control
sending_state = {
    'is_sending': False,
    'should_stop': False,
    'current_message': 0,
    'total_messages': 0,
    'current_number': '',
    'messages_sent_successfully': 0,
    'messages_failed': 0,
    'start_time': None,
    'estimated_time_remaining': 0,
    'current_recipient': '',
    'send_mode': '',
    'last_message_sent': '',
    'sending_speed': 0  # messages per minute
}

# State for reply monitoring - optimized for current batch processing
reply_state = {
    'monitoring': False,
    'target_recipient': None,  # Specific recipient to monitor for duplicates (username or user_id)
    'found_matches': {},  # {recipient_id: {pattern: number}} - patterns already processed per recipient
    'group_numbers': {},  # {number: [{'peer_id': id, 'access_hash': hash, 'msg_id': id, 'pattern': pattern}]}
    'processed_messages': set(),  # Track processed (peer_id, msg_id) tuples to avoid reprocessing
    'replies_received': {},  # {recipient_id: [list of reply messages]} - track replies per recipient
    'duplicate_replies': {},  # {recipient_id: {number: count}} - track duplicate counts per recipient
    'sending_start_times': {}  # {recipient_id: timestamp} - when sending started to each recipient
}

# Global lock to prevent concurrent Telegram client access
telegram_lock = threading.Lock()

# Add a proper monitoring control system
monitoring_thread = None
monitoring_stop_event = threading.Event()
monitoring_client = None

# Shared CSS styles
SHARED_STYLES = """
        body {
            margin: 0;
            padding: 20px;
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #0c0c3b, #1a1a5e, #0f0f4f); /* Blue anime night sky gradient */
            background-size: 400% 400%;
            animation: animeWave 15s ease infinite; /* Subtle anime-style wave animation */
            color: white;
            min-height: 100vh;
        }
        @keyframes animeWave {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .container { max-width: 600px; margin: 0 auto; background: rgba(0,0,0,0.5); padding: 20px; border-radius: 10px; }
        input, textarea, button { width: 100%; padding: 10px; margin: 10px 0; border: none; border-radius: 5px; }
        textarea { height: 100px; }
        button { background: #4a90e2; color: white; cursor: pointer; }
        button:hover { background: #357abd; }
        .error { color: #ff6b6b; }
        .success { color: #51cf66; }
        .nav-link { display: inline-block; padding: 10px 15px; margin: 5px; background: #2c2c54; border-radius: 5px; text-decoration: none; color: white; }
        .nav-link:hover { background: #40407a; }
        .stop-button { background: #e74c3c !important; margin-top: 10px; }
        .stop-button:hover { background: #c0392b !important; }
        .stop-button:disabled { background: #666 !important; cursor: not-allowed; }
        .sending-status { padding: 15px; margin: 15px 0; border-radius: 8px; background: rgba(255,255,255,0.1); display: none; border-left: 4px solid #4a90e2; }
        .progress-info { color: #51cf66; font-weight: bold; margin: 8px 0; }
        .progress-bar { width: 100%; height: 20px; background: rgba(255,255,255,0.2); border-radius: 10px; margin: 10px 0; overflow: hidden; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #4a90e2, #51cf66); transition: width 0.3s ease; }
        .status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin: 15px 0; }
        .status-item { background: rgba(255,255,255,0.1); padding: 10px; border-radius: 5px; text-align: center; }
        .status-value { font-size: 18px; font-weight: bold; color: #51cf66; }
        .pulse { animation: pulse 2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .error-count { color: #ff6b6b !important; }
        .success-count { color: #51cf66 !important; }
"""

# Authentication page template
AUTH_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Telegram Authentication</title>
    <style>""" + SHARED_STYLES + """</style>
</head>
<body>
    <div class="container">
        <h1>Telegram File Sender</h1>
        <p>Please authenticate with Telegram to continue</p>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <p class="{{ category }}">{{ message }}</p>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        {% if not code_requested %}
            <form method="POST" action="/request_code">
                <h2>Connect to Telegram</h2>
                <input type="text" name="phone" placeholder="Enter phone number with country code (e.g., +1234567890)" required>
                <button type="submit">Request Login Code</button>
            </form>
        {% else %}
            <form method="POST" action="/login">
                <h2>Enter Telegram Code</h2>
                <p>Code sent to: {{ phone_number }}</p>
                <input type="text" name="code" placeholder="Enter code from Telegram app" required>
                <button type="submit">Login</button>
            </form>
        {% endif %}
    </div>
</body>
</html>
"""

# Dashboard page template  
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Telegram File Sender - Dashboard</title>
    <style>""" + SHARED_STYLES + """</style>
</head>
<body>
    <div class="container">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
            <h1>File Sender Dashboard</h1>
            <a href="/logout" class="nav-link">Logout</a>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <p class="{{ category }}">{{ message }}</p>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <form method="POST" enctype="multipart/form-data" action="/upload">
            <h2>Upload CSV/TXT or Enter Data</h2>
            <label>CSV or TXT File:</label>
            <input type="file" name="file" accept=".csv,.txt">
            <label>Or Enter Manual Data:</label>
            <textarea name="manual_data" placeholder="Enter data line by line (one per line)"></textarea>
            <label>Recipient:</label>
            <input type="text" name="recipient" placeholder="@username or @botname" required>
            
            <label>Send Mode:</label>
            <div style="margin: 10px 0;">
                <input type="radio" id="columns" name="send_mode" value="columns" checked>
                <label for="columns" style="display: inline; margin-left: 5px; margin-right: 20px;">Column by Column (each column separately)</label>
                <input type="radio" id="rows" name="send_mode" value="rows">
                <label for="rows" style="display: inline; margin-left: 5px;">Row by Row (combine columns per row)</label>
            </div>
            
            <button type="submit" id="sendButton">Send to Recipient</button>
            
            <div id="sendingStatus" class="sending-status">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h3 style="margin: 0; color: #4a90e2;" id="statusText">📤 Sending Messages...</h3>
                    <button type="button" id="stopButton" class="stop-button pulse">🛑 Stop Sending</button>
                </div>
                
                <div style="margin: 15px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
                        <span id="progressInfo">Progress: 0/0</span>
                        <span id="progressPercent">0%</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progressFill" style="width: 0%;"></div>
                    </div>
                </div>
                
                <!-- Prominent Total Messages Sent Display -->
                <div style="background: linear-gradient(135deg, #27ae60, #2ecc71); border-radius: 12px; padding: 20px; margin: 15px 0; text-align: center; box-shadow: 0 4px 15px rgba(39, 174, 96, 0.3);">
                    <div style="color: white; font-size: 14px; margin-bottom: 5px; font-weight: 500;">📨 TOTAL MESSAGES SENT</div>
                    <div style="color: white; font-size: 36px; font-weight: bold; font-family: 'Arial', sans-serif;" id="totalMessagesSent">0</div>
                    <div style="color: rgba(255,255,255,0.9); font-size: 12px; margin-top: 5px;">This Session</div>
                </div>

                <div class="status-grid">
                    <div class="status-item">
                        <div class="status-value success-count" id="successCount" style="font-size: 24px; color: #27ae60;">0</div>
                        <div>✅ Sent Successfully</div>
                    </div>
                    <div class="status-item">
                        <div class="status-value error-count" id="failedCount" style="font-size: 24px; color: #e74c3c;">0</div>
                        <div>❌ Failed</div>
                    </div>
                    <div class="status-item">
                        <div class="status-value" id="sendingSpeed" style="font-size: 20px; color: #3498db;">0</div>
                        <div>📊 Messages/min</div>
                    </div>
                    <div class="status-item">
                        <div class="status-value" id="timeRemaining" style="font-size: 20px; color: #9b59b6;">--:--</div>
                        <div>⏱️ Est. Remaining</div>
                    </div>
                </div>
                
                <div class="progress-info">
                    <strong>📱 Recipient:</strong> <span id="currentRecipient">--</span><br>
                    <strong>🔄 Mode:</strong> <span id="sendMode">--</span><br>
                    <strong>💬 Current:</strong> <span id="currentNumber">--</span><br>
                    <strong>📨 Last Sent:</strong> <span id="lastMessageSent" style="font-family: monospace; font-size: 12px;">--</span>
                </div>
            </div>
        </form>
        
        <!-- Reply Monitoring Section -->
        <div style="margin-top: 30px; padding-top: 20px; border-top: 2px solid rgba(255,255,255,0.2);">
            <h2>Reply Monitoring (Always Active)</h2>
            <p style="font-size: 14px; color: #ccc; margin-bottom: 15px;">
                ✅ <strong>Automatic monitoring is active!</strong> The system continuously monitors incoming replies and automatically responds when someone sends the same number twice. 
                The system searches through all your groups for matching numbers (first 4 + last 4 digits).
            </p>
            
            <!-- Target Recipient Form -->
            <div style="margin: 20px 0; padding: 15px; background: rgba(255,255,255,0.1); border-radius: 8px;">
                <h3 style="margin-top: 0; color: #4a90e2;">🎯 Set Target Recipient</h3>
                <p style="font-size: 13px; color: #ccc; margin: 10px 0;">Enter the username of the person whose replies you want to monitor for duplicates:</p>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <input type="text" id="targetRecipientInput" placeholder="@username (e.g., @john_doe)" style="flex: 1; padding: 8px;">
                    <button type="button" id="setTargetButton" style="width: auto; padding: 8px 15px; background: #27ae60;">Set Target</button>
                </div>
                <div id="targetStatus" style="margin-top: 10px; font-size: 12px;"></div>
            </div>
            
            <div style="display: flex; align-items: center; gap: 15px; margin: 15px 0; padding: 10px; background: rgba(39, 174, 96, 0.2); border-radius: 8px; border-left: 4px solid #27ae60;">
                <span style="color: #27ae60; font-size: 18px;">🟢</span>
                <span style="color: #27ae60; font-weight: bold;">Always Monitoring - No manual control needed</span>
            </div>
            
            <div id="monitoringStatus" class="sending-status" style="display: block;">
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0;">
                    <div class="progress-info">
                        <strong>Status:</strong> <span id="monitoringState">Not monitoring</span>
                    </div>
                    <div class="progress-info">
                        <strong>Total Replies:</strong> <span id="totalReplies">0</span>
                    </div>
                    <div class="progress-info">
                        <strong>Duplicates Found:</strong> <span id="duplicateCount">0</span>
                    </div>
                    <div class="progress-info">
                        <strong>Auto-Replies Sent:</strong> <span id="matchesFound">0</span>
                    </div>
                </div>
                <div class="progress-info">
                    <strong>Numbers in Groups:</strong> <span id="groupNumbersCount">0</span>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let sendingInterval;
        
        // Prevent form submission and handle via AJAX
        document.querySelector('form[action="/upload"]').addEventListener('submit', function(e) {
            e.preventDefault(); // Prevent page redirect
            
            const formData = new FormData(this);
            const recipient = formData.get('recipient');
            
            if (!recipient || recipient.trim() === '') {
                alert('Please enter a recipient.');
                return;
            }
            
            // Check if either file or manual data is provided
            const file = formData.get('file');
            const manualData = formData.get('manual_data');
            if ((!file || file.name === '') && (!manualData || manualData.trim() === '')) {
                alert('Please provide either a CSV file or manual data.');
                return;
            }
            
            // Disable the send button and show progress immediately
            document.getElementById('sendButton').disabled = true;
            document.getElementById('sendButton').textContent = 'Starting...';
            document.getElementById('sendingStatus').style.display = 'block';
            document.getElementById('statusText').textContent = '🚀 Starting message sending...';
            
            // Submit the form data via fetch
            fetch('/upload', {
                method: 'POST',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: formData
            })
            .then(response => {
                if (response.ok) {
                    return response.json();
                } else {
                    return response.json().then(data => {
                        throw new Error(data.message || 'Failed to start sending');
                    }).catch(() => {
                        throw new Error('Server error - please try again');
                    });
                }
            })
            .then(data => {
                if (data.status === 'success') {
                    // Show success message
                    document.getElementById('statusText').textContent = '📤 Sending Messages...';
                    // Start polling for status immediately with 0.5 second real-time updates
                    if (!sendingInterval) {
                        sendingInterval = setInterval(checkSendingStatus, 500);
                    }
                } else {
                    throw new Error(data.message || 'Failed to start sending');
                }
            })
            .catch(error => {
                console.error('Error starting send:', error);
                // Show error to user
                const errorMsg = error.message || 'Failed to start sending. Please try again.';
                document.getElementById('statusText').textContent = '❌ Error: ' + errorMsg;
                document.getElementById('statusText').style.color = '#e74c3c';
                setTimeout(() => {
                    document.getElementById('statusText').style.color = '#4a90e2';
                    document.getElementById('statusText').textContent = '📤 Sending Messages...';
                }, 5000);
                
                // Reset UI on error
                document.getElementById('sendButton').disabled = false;
                document.getElementById('sendButton').textContent = 'Send to Recipient';
                document.getElementById('sendingStatus').style.display = 'none';
            });
        });
        
        document.getElementById('stopButton').addEventListener('click', function() {
            if (confirm('Are you sure you want to stop sending messages?')) {
                fetch('/stop', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        document.getElementById('statusText').textContent = '🛑 Stopping...';
                        document.getElementById('stopButton').disabled = true;
                        document.getElementById('stopButton').textContent = '⏹️ Stopping...';
                        document.getElementById('stopButton').classList.remove('pulse');
                    }
                })
               