**\# Agent Operating Guide**

These instructions help turn human prompts into reliable, repeatable systems.

AI can guess.   
This system is designed to behave.

\---

**\# How This Project Works**

There are two important files:

\- \`instructions.md\` → Defines how the system should behave.  
\- \`project\_specs.md\` → Defines what we are building.

The agent must follow both.

\---

**\# Step 1: Define the Project First**

Before writing any code, you must:

1\. Create a file called \`project\_specs.md\`  
2\. Clearly define:  
  \- What the user can send as input  
  \- What workflows exist  
  \- What tools are being used (Telegram, Airtable, Modal, etc.)  
  \- What outputs are expected  
  \- Where data is stored  
  \- Where the system will be deployed  
  \- What “done” looks like  
3\. Show the file  
4\. Wait for approval

No code should be written before this file is approved.

\---

**\# How the Agent Is Structured**

The system has three layers:

**\#\# How this works (simple)**

**\- \*\*Instructions\*\* \= what we want to happen (in \`instructions/\`)**  
**\- \*\*Decision\*\* \= pick the right workflow based on the message**  
**\- \*\*Actions\*\* \= the real work (Python scripts in \`execution/\`)**

**The agent can plan, but it must execute by running the scripts in \`execution/\`.**  
**No one-off code.**

\---

**\# File Structure**

\- \`instructions/\` → Workflow descriptions (markdown files)  
\- \`execution/\` → Python scripts  
\- \`.tmp/\` → Temporary files (safe to delete)  
\- \`.env\` → Secret keys and API tokens  
\- \`project\_specs.md\` → Full project definition

Test data can be saved in \`.tmp/\` as CSV files.  
Live data should be saved to Airtable or Google Sheets.

\---

**\# Development Rules**

**\#\# Rule 1: Always Read First**  
Always read:  
\- \`instructions.md\`  
\- \`project\_specs.md\`

Before taking action.

\---

**\*\*\#\# Rule 2: Python for Scripts\*\***

By default use and prefer Python for building scripts. If another language or runtime would materially improve the implementation, explain why and request approval before using it.

\---

**\#\# Rule 3: Every Workflow Has Two Files**  
Each workflow must include:  
\- A markdown file in \`instructions/\`  
\- A matching Python file in \`execution/\`

Do not run code unless both exist.

\---

**\#\# Rule 4: Build in Small Pieces**

Never build everything at once.

Instead:

1\. Build one small part  
2\. Test it locally  
3\. Confirm it works  
4\. Then move to the next piece  
5\. Only connect parts after both work independently

\---

**\#\# Rule 5: Deployment Checklist (Modal)**

Before deploying:

1\. Test locally  
2\. Make sure all secret keys are in \`.env\`  
3\. Show the deployment command  
4\. Wait for approval  
5\. Deploy  
6\. Test the live version  
7\. Confirm it works end-to-end

\---

**\# When Something Breaks**

1\. Fix the issue  
2\. Improve the script so it doesn’t fail the same way again  
3\. Test again  
4\. Update instructions if needed

Errors are feedback.

Each fix should improve the system and make it more robust.

\---

**\# Response Format**

When replying, always use:

\- **\*\*Plan\*\*** (3–7 bullet points)  
\- **\*\*What I need from you\*\*** (if anything)  
\- **\*\*Next action\*\*** (one clear step)  
\- **\*\*Errors\*\*** (explained simply)

\---

**\# Core Principle**

Define clearly.   
Build in small steps.   
Test before moving on. 

Reliable systems are built intentionally.

