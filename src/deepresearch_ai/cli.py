"""CLI entry points for model smoke checks and graph/legacy research."""

import argparse
import logging
from collections.abc import Sequence

from pydantic import ValidationError

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.config.logging import configure_logging
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.llm import create_chat_model
from deepresearch_ai.services.research import ResearchService
from deepresearch_ai.services.workflow import GraphResearchService

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deepresearch-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke", help="Invoke the configured chat model once.")
    smoke.add_argument("--prompt", default="Reply with exactly: DeepResearch AI is ready.")
    research = commands.add_parser("research", help="Research a question with cited source excerpts.")
    research.add_argument("question")
    research.add_argument("--max-pages", type=int, default=None,
                          help="Maximum usable graph pages (1-20); page-attempt cap in legacy single mode.")
    research.add_argument("--format", choices=("markdown", "json"), default="markdown")
    research.add_argument("--workflow", choices=("graph", "single"), default="graph",
                          help="Use the sequential agent graph (default) or the Phase 2 pipeline.")
    args = parser.parse_args(argv)
    if args.command == "smoke" and not args.prompt.strip():
        parser.error("--prompt must not be blank")

    configure_logging()
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        if args.command == "research":
            request = ResearchRequest(
                query=args.question,
                max_sources=args.max_pages if args.max_pages is not None else settings.research_max_pages,
            )
            service = GraphResearchService(settings) if args.workflow == "graph" else ResearchService(settings)
            report = service.run(request)
            if args.format == "json":
                print(report.model_dump_json(indent=2))
            else:
                print(f"Status: {report.status}\n\n{report.body_markdown}")
                by_id = {source.source_id: source for source in report.sources}
                if report.citations:
                    print("\nSources:")
                    for label, source_id in report.citations.items():
                        source = by_id[source_id]
                        print(f"[{label}] {source_id}\n    {source.original_url}")
                        print(f"    Published: {source.published_at or 'unknown'}; "
                              f"modified: {source.modified_at or 'unknown'}; retrieved: {source.retrieved_at.isoformat()}")
                print("\nLimitations:")
                for limitation in report.limitations:
                    print(f"- {limitation}")
            return 0
        model = create_chat_model(settings)
        if settings.mock_mode:
            logger.info("MOCK_MODE enabled: using fixed sample responses; no API requests.")
        else:
            logger.info("Invoking provider=%s model=%s", settings.llm_provider, settings.llm_model)
        response = model.invoke(args.prompt)
        text = response.text
        if not text.strip():
            logger.error("The model returned no text. Try another model or prompt.")
            return 1
        print(text)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2
    except ValidationError as exc:
        # Avoid logging raw inputs: validation errors may include credentials.
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        logger.error("Invalid configuration or research input for: %s. Check .env.example.", fields)
        return 2
    except Exception as exc:
        # Provider exception messages can include request details or credentials.
        action = "Model invocation" if args.command == "smoke" else "Research execution"
        logger.error(
            "%s failed (%s). Check credentials, LLM_MODEL, "
            "network access, and provider availability.",
            action,
            type(exc).__name__,
        )
        return 1
    return 0
