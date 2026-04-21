"""JSON Schema for structured PR output from the code healer agent."""

PR_REPORT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "pr_title": {
                "type": "string",
                "description": "Concise PR title, e.g. 'Fix null pointer in auth middleware'",
            },
            "pr_body": {
                "type": "string",
                "description": "Markdown PR description with ## Summary, ## Root Cause, ## Changes, ## Testing sections",
            },
            "root_cause": {
                "type": "string",
                "description": "One-paragraph explanation of what caused the bug",
            },
            "fix_description": {
                "type": "string",
                "description": "One-paragraph explanation of how the fix works",
            },
            "files_changed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "change_type": {
                            "type": "string",
                            "enum": ["modified", "created", "deleted"],
                        },
                        "description": {
                            "type": "string",
                            "description": "What was changed in this file and why",
                        },
                    },
                    "required": ["path", "change_type", "description"],
                },
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence that this fix resolves the issue without regressions",
            },
            "issue_number": {
                "type": "number",
                "description": "The GitHub issue number being fixed",
            },
        },
        "required": [
            "pr_title",
            "pr_body",
            "root_cause",
            "fix_description",
            "files_changed",
            "confidence",
            "issue_number",
        ],
    },
}
