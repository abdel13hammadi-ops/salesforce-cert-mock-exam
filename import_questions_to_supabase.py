import json
from pathlib import Path

import streamlit as st
from sqlalchemy import create_engine, text


QUESTION_FOLDER = Path("questions")


def get_engine():
    return create_engine(st.secrets["SUPABASE_DB_URL"])


def normalize_difficulty(value):
    if not value:
        return "medium"

    value = str(value).strip().lower()

    if value in ["easy", "foundation", "beginner"]:
        return "easy"

    if value in ["hard", "high", "high difficulty", "challenge", "expert challenge", "advanced"]:
        return "hard"

    return "medium"


def get_question_type(question):
    q_type = question.get("type", "single").strip().lower()
    answers = question.get("answers", [])

    if q_type == "multiple" or len(answers) > 1:
        return "multiple"

    return "single"


def import_json_file(engine, file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        questions = json.load(file)

    imported_count = 0

    with engine.begin() as conn:
        for q in questions:
            question_text = q.get("question", "").strip()
            options = q.get("options", [])
            answers = q.get("answers", [])
            explanation = q.get("explanation", "").strip()

            if not question_text or not options or not answers or not explanation:
                print(f"Skipped incomplete question in {file_path.name}: ID {q.get('id')}")
                continue

            question_type = get_question_type(q)
            select_count = q.get("select_count")

            if question_type == "multiple" and not select_count:
                select_count = len(answers)

            result = conn.execute(
                text("""
                    INSERT INTO questions (
                        exam_name,
                        category,
                        difficulty,
                        question_text,
                        question_type,
                        select_count,
                        explanation,
                        is_active
                    )
                    VALUES (
                        :exam_name,
                        :category,
                        :difficulty,
                        :question_text,
                        :question_type,
                        :select_count,
                        :explanation,
                        TRUE
                    )
                    RETURNING id
                """),
                {
                    "exam_name": q.get("exam", file_path.stem),
                    "category": q.get("topic", "Uncategorized"),
                    "difficulty": normalize_difficulty(q.get("difficulty")),
                    "question_text": question_text,
                    "question_type": question_type,
                    "select_count": select_count,
                    "explanation": explanation,
                },
            )

            question_id = result.scalar_one()

            for index, option_text in enumerate(options):
                label = chr(65 + index)
                is_correct = option_text in answers

                conn.execute(
                    text("""
                        INSERT INTO answer_options (
                            question_id,
                            option_label,
                            option_text,
                            is_correct
                        )
                        VALUES (
                            :question_id,
                            :option_label,
                            :option_text,
                            :is_correct
                        )
                    """),
                    {
                        "question_id": question_id,
                        "option_label": label,
                        "option_text": option_text,
                        "is_correct": is_correct,
                    },
                )

            imported_count += 1

    print(f"Imported {imported_count} questions from {file_path.name}")


def main():
    engine = get_engine()

    json_files = sorted(QUESTION_FOLDER.glob("*.json"))

    if not json_files:
        print("No JSON files found in the questions folder.")
        return

    for file_path in json_files:
        import_json_file(engine, file_path)

    print("Import completed.")


if __name__ == "__main__":
    main()
