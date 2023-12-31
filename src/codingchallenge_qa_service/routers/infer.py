import asyncio
from enum import Enum
from codingchallenge_qa_service.logging import getLogger
from typing import Any, Dict, Optional, Union
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Request, Response, HTTPException

from codingchallenge_qa_service.models.paraphrase_models import ParaphraseServiceRequest, ParaphraseServiceResponse
from codingchallenge_qa_service.models.qa_models import Language
from codingchallenge_qa_service.models.infer_request import InferRequest
from codingchallenge_qa_service.models.infer_response import InferResponse
from codingchallenge_qa_service.measure import Clock, Measure

logger = getLogger(name=__name__)


router = APIRouter()


class ResponseType(str, Enum):
    BASE_ANSWER = "base_answer"
    GS_ANSWER = "gs_answer"
    INVALID_NO_ES_DOCS_FOUND = "invalid_no_es_docs_found"
    INVALID_MODEL_ANSWER = "invalid_model_answer"
    SENSITIVE_CONTENT_ANSWER = "sensitive_content_answer"
    SENSITIVE_CONTENT_QUESTION = "sensitive_content_question"


def _check_answer_validity(answer: str, language: Language) -> bool:
    unknown_response = ["unknown", "unknown."] if language == Language.EN else ["unbekannt", "unbekannt."]
    if answer is None or answer == "":
        return False
    elif answer.lower() in unknown_response:
        return False
    return True


def _clean_question(question: str) -> str:
    # preprocess query (remove extra whitespaces)
    cleaned_question = question.strip()
    return cleaned_question


def _clean_answer(answer: str) -> str:
    cleaned_answer = answer.strip()
    return cleaned_answer


async def _run_sensitive_content_detection(request, query: str, user_id: str) -> Dict[str, str]:
    try:
        scd_response = await request.app.state.services.sensitive_content_detection_service.run(
            query=query, user_id=user_id
        )
    except Exception as e:
        request.state.transaction.record({"error": "Exception in Sensitive Content Detection.", "exc_info": e})
        logger.error("Exception in Sensitive Content Detection: ", exc_info=e)
        raise HTTPException(status_code=500, detail="Sensitive Content Detection Exception.")
    return scd_response


async def _question_has_sensitive_content(request: Request, user_id: str, question: str) -> bool:
    time_sensitive_content_detection: Clock = Measure.start_clock()
    scd_response = await _run_sensitive_content_detection(request, question, user_id)
    request.state.transaction.record(
        {
            "pipeline_steps": {
                "sensitive_content_detection_question": {
                    "sensitivity": scd_response["sensitivity"],
                    "model_name": scd_response["model_name"],
                    "duration": time_sensitive_content_detection.stop(),
                }
            },
            "question_sensitivity": scd_response["sensitivity"],
        }
    )

    logger.info(
        f"Classified question as {scd_response['sensitivity']}.",
        extra={"context": {"question": question, "sensitivity": scd_response["sensitivity"]}},
    )
    return scd_response["sensitivity"] != "SAFE"


async def _answer_has_sensitive_content(request: Request, user_id: str, answer: str) -> bool:
    time_sensitive_content_detection: Clock = Measure.start_clock()
    scd_response = await _run_sensitive_content_detection(request, answer, user_id)
    request.state.transaction.record(
        {
            "pipeline_steps": {
                "sensitive_content_detection_answer": {
                    "sensitivity": scd_response["sensitivity"],
                    "model_name": scd_response["model_name"],
                    "duration": time_sensitive_content_detection.stop(),
                }
            },
            "answer_sensitivity": scd_response["sensitivity"],
        }
    )
    logger.info(
        f"Classified answer as {scd_response['sensitivity']}.",
        extra={"context": {"answer": answer, "sensitivity": scd_response["sensitivity"]}},
    )
    return scd_response["sensitivity"] != "SAFE"


async def _get_prefiltered_documents_from_elasticsearch(
    request: Request, course_id: str, question: str, language: Language
) -> Dict:
    time_preselection: Clock = Measure.start_clock()
    try:
        prefiltered_doc = await request.app.state.services.prefiltering_service.run(
            query=question,
            language=language,
            coursebook_ids=[course_id],
        )
    except Exception as e:
        request.state.transaction.record({"error": "Exception in Prefiltering.", "exc_info": e})
        logger.error("Exception in Prefiltering: ", exc_info=e)
        raise HTTPException(
            status_code=412, detail="Failed to fetch prefiltered document from ElasticSearch."
        )
    request.state.transaction.record(
        {
            "pipeline_steps": {
                "preselection": {
                    "result": {"doc_id": prefiltered_doc["doc_id"]} if prefiltered_doc else {},
                    "duration": time_preselection.stop(),
                }
            },
        }
    )
    if not prefiltered_doc:
        logger.info("No relevant data found while prefiltering.")

    return prefiltered_doc


async def _get_answer_from_paraphrase(
    request: Request, course_id: str, cleaned_question: str
) -> Optional[ParaphraseServiceResponse]:
    time_paraphrase: Clock = Measure.start_clock()
    answer_from_paraphrase = None
    try:
        answer_from_paraphrase = await request.app.state.services.paraphrase_service.find_paraphrase(
            ParaphraseServiceRequest(
                question_content_str=cleaned_question,
                course_id=course_id,
            )
        )
    except Exception as e:
        logger.error("Failed to connect to the paraphrase service.", exc_info=e)
        request.state.transaction.record({"error": "Exception in Paraphrasing.", "exc_info": e})
    if answer_from_paraphrase:
        logger.info("Gold Standard answer retrieved from Paraphrase service.")
        request.state.transaction.record(
            {
                "pipeline_steps": {
                    "paraphrase": {
                        "result": answer_from_paraphrase,
                        "duration": time_paraphrase.stop(),
                    }
                },
            }
        )
    return answer_from_paraphrase


async def _get_model_inference_result(
    request: Request, user_id: str, question: str, doc: Dict, language: str
) -> Dict[str, Any]:
    try:
        time_qa: Clock = Measure.start_clock()
        inference_response = await request.app.state.services.infer_service.run(
            query=question, doc=doc, user_id=user_id, language=language
        )
        model_name = inference_response["model_context"]["model_name"]
        request.state.transaction.record(
            {
                "model_name": model_name,
                "answer": inference_response["answer"],
                "pipeline_steps": {
                    "inference": {
                        "model_name": model_name,
                        "inference_body": inference_response,
                        "duration": time_qa.stop(),
                    }
                },
            }
        )
        logger.info(
            "QA Service result ",
            extra={"context": {"model_name": model_name, "answer": inference_response["answer"]}},
        )
    except Exception as e:
        request.state.transaction.record({"error": "Exception in QAService.", "exc_info": e})
        logger.error("Exception in QAService: ", exc_info=e)
        raise HTTPException(status_code=400, detail="QAService Exception")

    return inference_response


@router.post("/infer")
async def infer(
    request: Request, request_body: InferRequest, background_tasks: BackgroundTasks
) -> Union[InferResponse, Response]:
    logger.debug("request received", extra={"request_body": request_body.dict()})
    request.state.transaction.should_store = True

    question_uuid = uuid4()
    cleaned_question = _clean_question(request_body.query)
    request.state.transaction.record(
        {
            "request_body": request_body,
            "client": request_body.client,
            "course_id": request_body.course_id,
            "user": request_body.user,
            "question_uuid": question_uuid,
            "question": cleaned_question,
            "language": request_body.language,
            "allow_annotation": request_body.allow_annotation,
        }
    )

    has_sensitive_content, paraphrase = await asyncio.gather(
        _question_has_sensitive_content(request, request_body.user.id, cleaned_question),
        _get_answer_from_paraphrase(request, request_body.course_id, cleaned_question),
    )

    if has_sensitive_content:
        return Response(status_code=400)

    if paraphrase:
        return InferResponse(
            answer=paraphrase.gs_answer_content_str,
            question=cleaned_question,
            answer_validity="valid",
            transaction_id=request.state.transaction.transaction_id,
            is_gs_answer=True,
            question_uuid=question_uuid,
        )

    prefiltered_doc = await _get_prefiltered_documents_from_elasticsearch(
        request,
        request_body.course_id,
        cleaned_question,
        request_body.language,
    )

    if not prefiltered_doc:
        return Response(status_code=404)

    model_inference_result = await _get_model_inference_result(
        request, request_body.user.id, cleaned_question, prefiltered_doc, request_body.language
    )

    cleaned_answer = _clean_answer(model_inference_result["answer"])

    if not _check_answer_validity(cleaned_answer, request_body.language):
        return Response(status_code=404)

    if await _answer_has_sensitive_content(request, request_body.user.id, cleaned_answer):
        return Response(status_code=400)

    return InferResponse(
            answer=cleaned_answer,
            question=cleaned_question,
            answer_validity="valid",
            transaction_id=request.state.transaction.transaction_id,
            is_gs_answer=False,
            question_uuid=question_uuid,
        )
