"""Agentic CI/CD for Azure Databricks and Azure DevOps.

Three agents work together:

* DataValidationAgent  - checks primary keys, fully-null columns and column-naming consistency.
* QaPromotionAgent     - moves feature branches into ``main`` (QA workspace) through a pull request.
* ReleaseAgent         - moves QA-certified commits into ``release`` (production workspace).
"""

__version__ = "0.1.0"
