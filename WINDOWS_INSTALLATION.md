# Astra AI Agent Windows Installation Guide

## 1. Introduction

Astra AI Agent is a Personal AI OS with a multi-provider router that provides local AI capabilities with support for multiple AI providers (Gemini, Groq, Cloudflare, Bedrock, OpenRouter, Mistral, Cerebras, SambaNova, Cohere, and Z.AI). It includes both a web interface and an isolated Agent Runtime environment.

**What this guide covers:** Installation, configuration, and first-time setup of Astra AI Agent on Windows 10 and Windows 11, including setup of the isolated Agent Runtime for secure AI operations.

**Supported Windows versions:** Windows 10 and Windows 11 (with WSL2 Ubuntu distribution)

## 2. Prerequisites

### 2.1 Python Installation
- **Required Python version:** Python 3.9 or later
- **Installation path:** Ensure Python is added to PATH during installation
- **Verification:** Run `python --version` or `python -V` in PowerShell or Command Prompt

### 2.2 Git Installation (Optional but Recommended)
- **Git for Windows:** Install from https://git-scm.com/download/win
- **Why Git matters:** Enables cloning the repository and proper package management
- **Verification:** Run `git --version`

### 2.3 WSL2 Ubuntu Installation (Required for Agent Runtime)
- **Prerequisite:** Windows 10/11 with virtualization support
- **Installation:** Run these commands in PowerShell as Administrator:
```powershell
# Install WSL2
wsl --install -d Ubuntu

# Set WSL2 as default version
wsl --set-version Ubuntu 2

# Launch Ubuntu for the first time
wsl
```

> **Note:** Astra never installs WSL automatically. You must install it manually first.

### 2.4 Other Dependencies
- **Web browser:** Chrome, Firefox, or Edge for accessing the web interface
- **Administrator privileges:** Required for WSL2 installation

## 3. Download and Install

### 3.1 Clone the Repository (Recommended Method)
Open PowerShell or Command Prompt in your desired location (e.g., `C:\projects`), then run:

```powershell
git clone https://github.com/mainnetwallet/Astra-AI-Agent.git
```

**Alternative ZIP download:** Download the ZIP from GitHub and extract it to your desired location.

### 3.2 Navigate to the Repository
```powershell
cd Astra-AI-Agent
```

### 3.3 Run Setup (Windows Command Prompt)
Run `setup.bat`:
```cmd
setup.bat
```

This will:
- Check for Python installation
- Install dependencies from `requirements.txt`
- Create `config.json` with default settings
- Create `data/` directory
- Display setup completion message

### 3.4 Run Setup (PowerShell)
Run `setup.ps1`:
```powershell
.\setup.ps1
```

This PowerShell setup provides a more detailed output and supports Windows 11 native features.

## 4. Configure AI Providers

### 4.1 Copy Environment Variables from Template
From your repository root, copy the example environment file:
```powershell
copy .env.example .env
```

### 4.2 Create Your API Keys File
Edit `.env` and add your API keys for at least one provider. For example:
```powershell
echo "GEMINI_API_KEYS=your_gemini_api_key_here" >> .env
echo "GROQ_API_KEYS=your_groq_api_key_here" >> .env
```

### 4.3 Required vs Optional Configuration
**Required:** At least one provider API key (Gemini, Groq, Cloudflare, Bedrock, OpenRouter, Mistral, Cerebras, SambaNova, Cohere, or Z.AI)

**Optional but recommended:** Gateway keys for additional routing flexibility

### 4.4 Configure Model Settings
In `.env`, you can set model preferences for each provider. For example:
```powershell
echo "GEMINI_MODELS=gemini-3.7-flash,gemini-3.6-flash" >> .env
echo "GROQ_MODELS=openai/gpt-oss-120b,openai/gpt-oss-20b" >> .env
```

### 4.5 Environment Variable Categories
The `.env.example` contains these sections:
- **API KEYS:** Provider authentication keys
- **MODELS & ENDPOINTS:** Available models and API endpoints
- **SETTINGS:** Server configuration (port, bind address, etc.)

### 4.6 Security Best Practices
- **Never commit `.env` to Git:** Add it to `.gitignore`
- **Environment protection:** Keep API keys in a secure location
- **Environment separation:** Use different `.env` files for development vs production

### 4.7 Verify Configuration
After editing `.env`, restart Astra to load the new configuration. Check for any error messages about missing or invalid API keys.

## 5. Start Astra

### 5.1 Recommended Startup Method (PowerShell)
From your repository root in PowerShell:
```powershell
.\start.ps1
```

### 5.2 Alternative Startup Methods

#### 5.2.1 Windows Command Prompt
```cmd
start.bat
```

#### 5.2.2 Custom Port Configuration
Set a custom port before starting:
```powershell
$env:PORT="9000"
.\start.ps1
```

#### 5.2.3 Disable Auto-browser
```powershell
$env:NO_BROWSER="1"
.\start.ps1
```

#### 5.2.4 Bind to Specific Address
```powershell
$env:BIND="0.0.0.0"
.\start.ps1
```

#### 5.2.5 Enable Token Protection
```powershell
echo "ASTRA_TOKEN=your_secure_token_here" >> .env
.\start.ps1
```

#### 5.2.6 Enable Scheduler
```powershell
$env:ASTRA_SCHEDULER="1"
.\start.ps1
```

### 5.3 Default Configuration
When starting Astra normally:
- **Default port:** 8787
- **Default bind:** 127.0.0.1 (loopback only)
- **Web interface:** Automatically opens in your default browser
- **API access:** Available at `http://localhost:8787/`

### 5.4 Web Interface Access
Open your browser and navigate to:
```
http://localhost:8787/
```

**Default login:** No authentication required

### 5.5 Stopping Astra Safely
To stop Astra running in Command Prompt/PowerShell:
```cmd
Ctrl+C
```

Or in PowerShell:
```powershell
Ctrl+C
```

The application will display a confirmation message and shut down gracefully.

## 6. First-Time Usage

### 6.1 Access the Assistant
1. Open your browser to `http://localhost:8787/`
2. You'll see the Astra AI Agent interface
3. Look for the chat interface or command prompt

### 6.2 Test an AI Provider
Send a simple test message like:
```
Hello, how are you?
```

### 6.3 Verify Provider Health
Check the system status by looking for provider connection indicators or status messages in the interface.

### 6.4 Send Your First Message
Try a basic query to verify the setup:
```
What is the current date and time?
```

## 7. Windows Agent Runtime Setup

### 7.1 Understanding Astra Runtime vs Web Application
- **Astra Web Application:** Browser-based interface for basic AI interactions
- **Astra Agent Runtime:** Isolated Linux environment for secure agent operations with full system access

### 7.2 WSL2 Ubuntu Requirements
Ensure WSL2 Ubuntu is installed and running:

```powershell
# Check if WSL2 is installed
wsl --status

# Launch Ubuntu for the first time (if not already running)
wsl
```

### 7.3 Installation and Initialization
Run these commands from your repository root:

#### 7.3.1 Create Runtime
```powershell
# Create a new runtime (replace "my-runtime" with your desired name)
# This will be done automatically when Astra detects the need for isolated execution
```

#### 7.3.2 Start Runtime
```powershell
# The runtime starts automatically when you need it
# Check runtime status in Astra's interface
```

### 7.4 Runtime Verification
Verify your Agent Runtime is working by:

1. **Checking status** in the Astra interface
2. **Testing basic commands** like `ls` or `pwd`
3. **Verifying file access** to runtime directories

### 7.5 Runtime Unavailability and Host Terminal Approval

#### 7.5.1 When Runtime is Unavailable
If the Agent Runtime cannot start:
- Astra will display "Agent Runtime unavailable"
- You can use host terminal fallback for specific operations
- Check `host_terminal_request` for approval opportunities

#### 7.5.2 Host Terminal Approval Behavior
- **Approval required:** Certain operations need explicit user approval
- **Scoped approvals:** Each approval is specific to one command and conversation
- **Temporary access:** Approvals expire after 15 minutes by default
- **Secure fallback:** Host terminal provides necessary functionality when runtime is unavailable

## 8. Troubleshooting

### 8.1 Python or Pip Not Recognized

**Problem:** Python installation issues

**Solution:**
1. Install Python from https://python.org
2. Ensure "Add python.exe to PATH" during installation
3. Restart Command Prompt/PowerShell
4. Verify with: `python --version`

### 8.2 Missing Dependencies

**Problem:** pip install failures

**Solution:**
1. Run setup again: `setup.bat` or `.\setup.ps1`
2. Manually install: `python -m pip install -r requirements.txt`
3. Check pip version: `python -m pip --version`

### 8.3 Port Conflicts

**Problem:** Port 8787 is already in use

**Solution:**
1. Change port in `.env`: `PORT=8788`
2. Or set environment variable before starting:
```powershell
$env:PORT="8788"
.\start.ps1
```

### 8.4 Invalid API Keys

**Problem:** Provider authentication failures

**Solution:**
1. Verify API keys are correct and not expired
2. Check key format and permissions
3. Test with a different provider if available
4. Refer to provider documentation for key setup

### 8.5 Provider Connection Failures

**Problem:** Cannot connect to AI provider

**Solution:**
1. Check internet connectivity
2. Verify provider API endpoints are accessible
3. Review error messages in the Astra interface
4. Try a different provider from `.env.example`

### 8.6 PowerShell Execution-Policy Restrictions

**Problem:** Execution policy blocks script execution

**Solution:**
```powershell
# Set execution policy for current user only
set-executionpolicy -executionpolicy RemoteSigned -scope CurrentUser
```

### 8.7 WSL2 or Ubuntu Missing

**Problem:** Agent Runtime requires WSL2

**Solution:**
1. Install WSL2: `wsl --install -d Ubuntu`
2. Set WSL2 version: `wsl --set-version Ubuntu 2`
3. Launch Ubuntu: `wsl`
4. Restart Astra after WSL2 setup

### 8.8 Agent Runtime Unavailability

**Problem:** Runtime fails to start

**Solution:**
1. Check WSL2 installation: `wsl --list`
2. Verify Ubuntu distribution is accessible
3. Check runtime status in Astra interface
4. Try manual runtime creation if needed

## 9. Updating Astra

### 9.1 Safe Update Process
1. **Backup your `.env` file:** Copy `.env` to `.env.backup`
2. **Backup data files:** Protect `data/` and `config.json`
3. **Protect user data:** Ensure workspace data is backed up

### 9.2 Update Steps
1. Navigate to repository root
2. Fetch latest changes:
```powershell
git fetch origin
```
3. Checkout main branch:
```powershell
git checkout main
```
4. Pull updates:
```powershell
git pull origin main
```

### 9.3 Restore Configuration
1. Replace `.env` if modified (restore from backup)
2. Preserve custom `config.json` settings
3. Keep existing `data/` directory contents

### 9.4 Testing After Update
1. Run setup again if dependencies changed: `setup.bat`
2. Start Astra: `.\start.ps1`
3. Verify all functionality works correctly

## 10. Useful Commands

### 10.1 Installation and Setup
- **Clone repo:** `git clone https://github.com/mainnetwallet/Astra-AI-Agent.git`
- **Navigate:** `cd Astra-AI-Agent`
- **Install dependencies:** `setup.bat` or `.\setup.ps1`
- **Configure:** Edit `.env` file

### 10.2 Starting Astra
- **Standard start:** `.\start.ps1`
- **Command Prompt:** `start.bat`
- **Custom port:** `$env:PORT="9000"; .\start.ps1`
- **No browser:** `$env:NO_BROWSER="1"; .\start.ps1`

### 10.3 Runtime Management
- **Check WSL status:** `wsl --status`
- **List distributions:** `wsl --list`
- **Runtime status:** Check in Astra interface
- **Host terminal approval:** Request via Astra interface

### 10.4 Configuration Management
- **Edit config:** Use any text editor on `config.json`
- **View logs:** Check Astra interface
- **Provider settings:** Modify `.env` file

### 10.5 Testing and Verification
- **Python version:** `python --version`
- **Git version:** `git --version`
- **WSL status:** `wsl --status`
- **Port check:** `netstat -an | findstr 8787`

## 11. Additional Documentation

### 11.1 Main Documentation
- **README.md:** Basic project information and overview
- **ARCHITECTURE.md:** Technical architecture details

### 11.2 Agent Runtime Documentation
- **AGENT_RUNTIME.md:** Detailed Agent Runtime setup and usage

### 11.3 Access Links
- **Web Interface:** `http://localhost:8787/`
- **Main README:** [README.md](README.md)
- **Architecture Guide:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **Agent Runtime:** [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md)