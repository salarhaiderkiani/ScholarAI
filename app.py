
import streamlit as st
import pandas as pd
import json
import csv
import io
import os
import re
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from html import escape
from math import isclose

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

st.set_page_config(
    page_title="ScholarAI | Smart Learning",
    page_icon="🎓",
    layout="wide"
)

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"
GEMINI_MODEL = "gemini-3.5-flash-lite"
DB_PATH = Path(__file__).with_name("scholarai_progress.db")

# ---------------- DESIGN (UNCHANGED) ----------------

st.markdown("""
<style>
.stApp {
    background: linear-gradient(135deg,#f3f6ff,#edfcf8);
}
[data-testid="stSidebar"] {
    background: #e9efff;
}
.block-container {
    max-width: 1180px;
    padding-top: 1.5rem;
}
h1,h2,h3 {
    color: #213c80 !important;
}
.hero {
    padding: 29px;
    border-radius: 20px;
    background: linear-gradient(110deg,#333c9a,#087f91);
    color: white;
    margin-bottom: 20px;
}
.hero h1 {
    color: white !important;
    font-size: 2.4rem;
}
.hero p {
    color: #eaf9ff;
}
[data-testid="stMetric"] {
    background: white;
    padding: 15px;
    border: 1px solid #d9e2f5;
    border-radius: 14px;
}
div.stButton>button[kind="primary"] {
    background: linear-gradient(90deg,#4b46cb,#078e9b);
    color: white;
    border: 0;
    border-radius: 12px;
}
div.stButton>button,
div.stDownloadButton>button {
    border-radius: 12px;
}
</style>
""", unsafe_allow_html=True)

# ---------------- DATABASE ----------------

with sqlite3.connect(DB_PATH) as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY,
            date TEXT,
            subject TEXT,
            topic TEXT,
            score INTEGER,
            total INTEGER,
            percentage REAL
        )
    """)


def save_progress(subject, topic, score, total):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT INTO quizzes
            (date,subject,topic,score,total,percentage)
            VALUES (?,?,?,?,?,?)""",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                subject,
                topic,
                score,
                total,
                round(score / total * 100, 2)
            )
        )


def get_history():
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(
            "SELECT * FROM quizzes ORDER BY id",
            con
        )


# ---------------- SESSION ----------------

for key, default in {
    "notes": "",
    "cards": [],
    "questions": [],
    "quiz_result": None,
    "quiz_version": 0,
    "quiz_subject": "",
    "quiz_topic": "",
    "quiz_submitted": False
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------- AI HELPERS ----------------

def extract_json(text):
    text = text.strip()
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.I
    )
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError(
        "The AI did not return valid structured data."
    )


@st.cache_data(show_spinner=False, ttl=86400, max_entries=80)
def generate_cached(
    engine,
    model,
    prompt,
    json_mode,
    max_tokens,
    api_key
):
    if engine == "Online Gemini":
        if not api_key:
            raise ValueError(
                "Enter your Gemini API key in the sidebar."
            )

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
        )

        generation = {
            "temperature": 0.15,
            "maxOutputTokens": max_tokens
        }

        if json_mode:
            generation["responseMimeType"] = "application/json"

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": generation
        }

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        }

    else:
        url = OLLAMA_URL
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "20m",
            "options": {
                "temperature": 0.15,
                "num_predict": max_tokens,
                "num_ctx": 4096
            }
        }

        if json_mode:
            payload["format"] = "json"

        headers = {
            "Content-Type": "application/json"
        }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=240
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                "AI API limit reached. Wait and retry, "
                "or switch to Offline Ollama."
            ) from error

        if error.code in (400, 401, 403, 404):
            raise RuntimeError(
                f"AI request failed (HTTP {error.code}). "
                "Check your API key and model access."
            ) from error

        raise RuntimeError(
            f"AI service returned HTTP {error.code}."
        ) from error

    except (
        urllib.error.URLError,
        TimeoutError
    ) as error:
        raise RuntimeError(
            "Could not reach the AI service. "
            "Check your internet connection or Ollama."
        ) from error

    if engine == "Online Gemini":
        candidates = result.get("candidates", [])

        if not candidates:
            raise ValueError(
                "Gemini returned no response."
            )

        parts = candidates[0].get(
            "content", {}
        ).get("parts", [])

        answer = "\n".join(
            part.get("text", "")
            for part in parts
        ).strip()

    else:
        answer = result.get(
            "response", ""
        ).strip()

    if not answer:
        raise ValueError(
            "The AI response was empty."
        )

    return extract_json(answer) if json_mode else answer


# ---------------- DOCUMENTS ----------------

def read_document(upload):
    if upload is None:
        return ""

    raw = upload.getvalue()
    ext = Path(upload.name).suffix.lower()

    if ext in (".txt", ".md"):
        return raw.decode("utf-8-sig")

    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        result = "\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

        if not result.strip():
            raise ValueError(
                "No selectable PDF text found. "
                "Scanned PDFs are not yet supported."
            )
        return result

    if ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(raw))
        return "\n".join(
            paragraph.text
            for paragraph in doc.paragraphs
        )

    raise ValueError("Unsupported document format.")


# ---------------- PDF EXPORT ----------------

def make_pdf(content):
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=42,
        rightMargin=42
    )

    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        name="ScholarHeading",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#263d8b"),
        fontSize=13,
        spaceBefore=12,
        spaceAfter=7
    ))

    styles.add(ParagraphStyle(
        name="ScholarParagraph",
        parent=styles["BodyText"],
        fontSize=10,
        leading=15,
        spaceAfter=7
    ))

    story = [
        Paragraph(
            "ScholarAI Study Notes",
            styles["Title"]
        ),
        Spacer(1, 16)
    ]

    for raw in content.splitlines():
        line = raw.strip()

        if not line:
            story.append(Spacer(1, 5))
            continue

        heading = line.startswith("#")

        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"\$(.*?)\$", r"\1", line)

        for old, new in {
            "π": "pi",
            "√": "sqrt",
            "²": "^2",
            "³": "^3",
            "°": " degrees",
            "θ": "theta",
            "×": "x",
            "÷": "/",
            "−": "-"
        }.items():
            line = line.replace(old, new)

        line = line.encode(
            "latin-1",
            errors="replace"
        ).decode("latin-1")

        story.append(Paragraph(
            escape(line),
            styles[
                "ScholarHeading"
                if heading
                else "ScholarParagraph"
            ]
        ))

    doc.build(story)
    return buffer.getvalue()


# ---------------- MATHEMATICAL CHECK ----------------

def verify_circle_calculation(question, answer, explanation):
    text = question.lower().replace(" ", "")

    if "circumference" in text and "radius" in text:
        match = re.search(
            r"circumference(?:of|is|=|:)"
            r"(\d+(?:\.\d+)?)(?:π|pi)",
            text
        )

        if match:
            value = float(match.group(1))
            radius = value / 2

            answer = f"{radius:g}"
            explanation = (
                f"C = 2πr. Therefore r = "
                f"{value:g}π / 2π = {radius:g}."
            )

    return answer, explanation


# ---------------- STRUCTURED QUESTIONS ----------------

def create_questions(
    subject,
    topic,
    source,
    difficulty,
    count,
    engine,
    model,
    api_key
):
    prompt = f"""
You are creating a reliable educational quiz.

Subject: {subject}
Topic: {topic}
Difficulty: {difficulty}
Requested questions: {count}

Source:
{source[:5500] if source else "Use established subject knowledge."}

Return ONLY a JSON object:
{{
 "questions": [
   {{
     "question": "Question",
     "answer": "Accurate answer",
     "explanation": "Short worked explanation"
   }}
 ]
}}

Requirements:
- Produce exactly {count} different questions.
- Avoid ambiguous questions.
- Verify each numerical calculation.
- Answers must match explanations.
- For circumference C = 20π, radius is 10.
- Do not invent information from source notes.
- Prefer clear, short answers.
"""

    response = generate_cached(
        engine, model, prompt, True,
        1300 if count <= 5 else 2300,
        api_key
    )

    items = []

    for item in response.get("questions", []):
        if not isinstance(item, dict):
            continue

        q = str(item.get("question", "")).strip()
        a = str(item.get("answer", "")).strip()
        e = str(item.get("explanation", "")).strip()

        if q and a:
            a, e = verify_circle_calculation(q, a, e)

            items.append({
                "question": q,
                "answer": a,
                "explanation": e
            })

    if not items:
        raise ValueError(
            "No valid questions generated. Please retry."
        )

    return items[:count]


# ==============================================
# SMARTER QUIZ MARKING — MAIN NEW FEATURE
# ==============================================

def normalize(value):
    """Normalize simple equivalent notation."""
    text = str(value).casefold().strip()

    text = re.sub(r"\bpie\b", "pi", text)

    replacements = {
        "π": "pi",
        "²": "^2",
        "³": "^3",
        "−": "-",
        "°": "degrees",
        "×": "*"
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return re.sub(r"\s+", "", text)


def numeric_answer(value):
    """
    Read a simple number, optionally followed by pi,
    and optional ordinary units.

    Examples:
      180
      180 degrees
      36 pi cm^3
      36 pie
      14 meters
      5 sides

    Returns None for complicated expressions.
    """
    text = str(value).casefold().strip()
    text = re.sub(r"\bpie\b", "pi", text)

    for old, new in {
        "π": "pi",
        "²": "^2",
        "³": "^3",
        "−": "-",
        "°": " degrees",
    }.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text)

    pattern = (
        r"^([-+]?(?:\d+(?:\.\d*)?|\.\d+))"
        r"\s*(pi)?"
        r"\s*(.*)$"
    )

    match = re.fullmatch(pattern, text)

    if not match:
        return None

    number = float(match.group(1))
    has_pi = bool(match.group(2))
    suffix = match.group(3).strip()

    # Only allow recognizable descriptive suffixes.
    # Reject extra numbers or mathematical operators.
    if suffix and not re.fullmatch(
        r"[a-zA-Zµμ^0-9 /\-]+",
        suffix
    ):
        return None

    if suffix and not re.search(r"[a-zA-Zµμ]", suffix):
        return None

    if suffix and re.search(r"\d", suffix):
        # Digits are allowed only as unit powers.
        # e.g. cm^3 or m^2
        without_powers = re.sub(r"\^\d+", "", suffix)
        if re.search(r"\d", without_powers):
            return None

    return (number, has_pi, suffix)


def safe_numeric_match(student, expected):
    """
    Accept the same numerical value even when the
    student omits a unit or writes 'pie' for pi.

    If both sides explicitly give units, units must
    match here; AI can assess other valid equivalents.
    """
    a = numeric_answer(student)
    b = numeric_answer(expected)

    if a is None or b is None:
        return False

    a_number, a_pi, a_unit = a
    b_number, b_pi, b_unit = b

    if a_pi != b_pi:
        return False

    if not isclose(
        a_number,
        b_number,
        rel_tol=1e-8,
        abs_tol=1e-9
    ):
        return False

    # Different explicit units require closer review.
    if a_unit and b_unit:
        simple_units = lambda x: re.sub(
            r"\s+",
            "",
            x
        ).replace(
            "centimeters", "cm"
        ).replace(
            "centimeter", "cm"
        ).replace(
            "meters", "m"
        ).replace(
            "meter", "m"
        ).replace(
            "degrees", "deg"
        ).replace(
            "degree", "deg"
        )

        if simple_units(a_unit) != simple_units(b_unit):
            return False

    return True


def mark_quiz(
    questions,
    responses,
    engine,
    model,
    api_key
):
    """
    First use reliable direct checks.
    Then send uncertain nonblank answers to AI.
    Never award points to a blank answer.
    """

    results = []
    needs_ai = []

    for index, (item, response) in enumerate(
        zip(questions, responses)
    ):
        response = str(response).strip()
        expected = item["answer"]

        if not response:
            results.append({
                "correct": False,
                "feedback": "No answer submitted."
            })
            continue

        if normalize(response) == normalize(expected):
            results.append({
                "correct": True,
                "feedback": "Correct answer."
            })
            continue

        if safe_numeric_match(response, expected):
            results.append({
                "correct": True,
                "feedback": (
                    "Correct numerical answer. "
                    "Equivalent notation or omitted "
                    "units accepted."
                )
            })
            continue

        results.append({
            "correct": False,
            "feedback": (
                "Could not verify automatically."
            )
        })

        needs_ai.append({
            "index": index,
            "question": item["question"],
            "expected_answer": expected,
            "explanation": item["explanation"],
            "student_answer": response
        })

    # Only ambiguous attempted answers go to AI.
    if needs_ai:
        prompt = f"""
You are a careful academic quiz marker.

Evaluate each student's answer using the question,
expected answer, and explanation.

Return JSON only:
{{
  "results": [
    {{
      "index": 0,
      "correct": true,
      "feedback": "One brief reason"
    }}
  ]
}}

Marking rules:
- Accept synonyms and equivalent wording.
- Accept equivalent mathematically correct answers.
- Accept missing units if units are clear from
  the question and the number is correct.
- Accept 'pie', 'pi', and π as equivalent.
- Reject wrong numerical values.
- Reject answers that only identify a related
  concept but not the actual correct concept.
- Example: 'acute' is not a right angle.
- Example: area 6 x 4 is 24, not 12.
- Example: a diameter passes through the
  circle's center; an arbitrary chord need not.
- Do not assume that the supplied expected answer
  is necessarily correct. Check it independently
  against the question and explanation.
- If the student's answer is ambiguous or
  insufficient, mark incorrect.
- Return each index exactly once.
- Be concise.

Student answers:
{json.dumps(needs_ai, ensure_ascii=False)}
"""

        try:
            reviewed = generate_cached(
                engine,
                model,
                prompt,
                True,
                min(
                    2200,
                    300 + len(needs_ai) * 160
                ),
                api_key
            )

            allowed = {
                row["index"]
                for row in needs_ai
            }

            reviewed_indices = set()

            for row in reviewed.get("results", []):
                if not isinstance(row, dict):
                    continue

                index = row.get("index")
                decision = row.get("correct")

                if (
                    type(index) is int
                    and index in allowed
                    and index not in reviewed_indices
                    and type(decision) is bool
                ):
                    results[index] = {
                        "correct": decision,
                        "feedback": str(
                            row.get(
                                "feedback",
                                "AI-reviewed answer."
                            )
                        )
                    }
                    reviewed_indices.add(index)

            for index in allowed - reviewed_indices:
                results[index]["feedback"] = (
                    "AI could not verify this answer. "
                    "Please review manually."
                )

        except Exception:
            for row in needs_ai:
                results[row["index"]]["feedback"] = (
                    "AI marking unavailable. "
                    "Answer not awarded automatically; "
                    "please review manually."
                )

    return results


# ---------------- HEADER (UNCHANGED) ----------------

st.markdown("""
<div class="hero">
  <h1>🎓 ScholarAI</h1>
  <p>Your personal AI study companion — notes,
  practice quizzes, flashcards and progress tracking.</p>
</div>
""", unsafe_allow_html=True)

# ---------------- SIDEBAR (UNCHANGED) ----------------

with st.sidebar:
    st.header("⚙️ AI Settings")

    engine = st.radio(
        "AI engine",
        ["Online Gemini", "Offline Ollama"],
        horizontal=False
    )

    if engine == "Online Gemini":
        api_key = st.text_input(
            "Gemini API key",
            value=os.getenv("GEMINI_API_KEY", ""),
            type="password",
            help="Never put this key in GitHub."
        )

        model = GEMINI_MODEL

        if api_key:
            st.success("API key entered")
        else:
            st.info("Enter your free Gemini API key.")

    else:
        api_key = ""
        model = OLLAMA_MODEL
        st.info("Runs on your own computer.")

    st.divider()
    st.header("📖 Study Settings")

    subject = st.selectbox(
        "Subject",
        [
            "Mathematics",
            "Physics",
            "Computer Science",
            "Business Management",
            "Chemistry",
            "Biology",
            "English",
            "French",
            "History",
            "Other"
        ]
    )

    level = st.selectbox(
        "Difficulty",
        [
            "Beginner",
            "Intermediate",
            "Advanced",
            "IB Diploma"
        ],
        index=3
    )

    topic = st.text_input(
        "Enter topic",
        placeholder="e.g. Geometry"
    )

    notes_input = st.text_area(
        "Paste notes (optional)",
        height=110
    )

    upload = st.file_uploader(
        "Upload notes (optional)",
        type=["txt", "md", "pdf", "docx"]
    )

    count = st.slider(
        "Number of quiz questions",
        1, 10, 5
    )

    st.caption(
        "Free online API usage is limited. "
        "AI responses can still contain mistakes."
    )

source = notes_input.strip()

if upload is not None:
    try:
        extracted = read_document(upload)
        source = (source + "\n" + extracted).strip()
        st.sidebar.success("Notes loaded.")
    except Exception as error:
        st.sidebar.error(str(error))

tabs = st.tabs([
    "📝 Study Notes",
    "🎯 Quiz",
    "🧠 Flashcards",
    "📊 Progress"
])

# ---------------- NOTES TAB (UNCHANGED) ----------------

with tabs[0]:
    st.subheader("Generate Study Notes")

    style = st.radio(
        "Notes style",
        [
            "Quick Revision",
            "Detailed",
            "Exam Preparation"
        ],
        horizontal=True
    )

    if st.button(
        "✨ Generate Notes",
        type="primary"
    ):
        if not topic.strip() and not source:
            st.warning("Enter a topic or provide notes.")

        else:
            prompt = f"""
You are an accurate educational tutor.

Subject: {subject}
Topic: {topic or "Uploaded study material"}
Level: {level}
Notes format: {style}

Source notes:
{source[:6500] if source else "No uploaded source."}

Write useful {style.lower()} study notes.

Important:
1. Begin with an accurate definition.
2. Divide broad topics into relevant subtopics.
3. Explain concepts instead of repeating definitions.
4. Include important formulas with variable meanings.
5. Include at least 2 worked examples where suitable.
6. Distinguish related formulas carefully.
7. For cylinders, distinguish curved surface area
   2πrh from total area 2πrh + 2πr².
8. Include common mistakes and actual practice questions.
9. End with a concise revision summary.
10. Do not invent unsupported source details.
11. Use Markdown headings and LaTeX math in $...$.
12. If uncertain about a fact, say so.

Avoid repetition and filler paragraphs.
"""

            try:
                with st.spinner("Preparing your notes..."):
                    st.session_state.notes = generate_cached(
                        engine,
                        model,
                        prompt,
                        False,
                        2400 if style == "Detailed" else 1350,
                        api_key
                    )

                st.success("Study notes generated.")

            except Exception as error:
                st.error(str(error))

    if st.session_state.notes:
        st.markdown(st.session_state.notes)

        left, right = st.columns(2)

        with left:
            st.download_button(
                "📄 Download PDF",
                data=make_pdf(st.session_state.notes),
                file_name="ScholarAI_Notes.pdf",
                mime="application/pdf"
            )

        with right:
            st.download_button(
                "📝 Download Markdown",
                data=st.session_state.notes,
                file_name="ScholarAI_Notes.md",
                mime="text/markdown"
            )

    st.caption(
        "Review AI-generated formulas before studying "
        "from or sharing these notes."
    )

# ---------------- QUIZ TAB (SMARTER GRADING) ----------------

with tabs[1]:
    st.subheader("AI Practice Quiz")

    if st.button(
        "🚀 Generate Quiz",
        type="primary"
    ):
        if not topic.strip() and not source:
            st.warning("Enter a topic or upload notes.")

        else:
            try:
                with st.spinner("Generating quiz..."):
                    new_questions = create_questions(
                        subject,
                        topic,
                        source,
                        level,
                        count,
                        engine,
                        model,
                        api_key
                    )

                st.session_state.questions = new_questions
                st.session_state.cards = new_questions.copy()
                st.session_state.quiz_version += 1
                st.session_state.quiz_result = None
                st.session_state.quiz_submitted = False
                st.session_state.quiz_subject = subject
                st.session_state.quiz_topic = (
                    topic or "Uploaded Notes"
                )

                st.success(
                    f"{len(new_questions)} questions ready."
                )

            except Exception as error:
                st.error(str(error))

    questions = st.session_state.questions

    if questions:
        st.write(f"**Questions:** {len(questions)}")

        with st.form(
            f"quiz_form_{st.session_state.quiz_version}"
        ):
            responses = []

            for index, item in enumerate(questions):
                st.markdown(
                    f"**Question {index + 1}: "
                    f"{item['question']}**"
                )

                response = st.text_input(
                    "Your answer",
                    key=(
                        f"answer_{st.session_state.quiz_version}"
                        f"_{index}"
                    )
                )

                responses.append(response)

            submitted = st.form_submit_button(
                "✅ Check Answers"
            )

        if submitted and not st.session_state.quiz_submitted:
            with st.spinner(
                "Checking answers, including "
                "equivalent wording..."
            ):
                results = mark_quiz(
                    questions,
                    responses,
                    engine,
                    model,
                    api_key
                )

            score = sum(
                item["correct"] for item in results
            )

            st.session_state.quiz_result = {
                "score": score,
                "items": results
            }

            st.session_state.quiz_submitted = True

            save_progress(
                st.session_state.quiz_subject,
                st.session_state.quiz_topic,
                score,
                len(questions)
            )

        if st.session_state.quiz_result:
            result = st.session_state.quiz_result

            st.metric(
                "Quiz Score",
                f"{result['score']}/{len(questions)}"
            )

            st.progress(
                result["score"] / len(questions)
            )

            for index, (question, check) in enumerate(
                zip(questions, result["items"])
            ):
                if check["correct"]:
                    st.success(
                        f"Question {index + 1}: Correct"
                    )
                else:
                    st.error(
                        f"Question {index + 1}: "
                        "Not marked correct"
                    )

                st.write(
                    "**Expected answer:**",
                    question["answer"]
                )

                st.write(
                    "**Explanation:**",
                    question["explanation"]
                )

                st.caption(check["feedback"])

            st.info(
                "ScholarAI accepts common numerical "
                "equivalents and uses AI to check "
                "other answers. AI grading is not "
                "perfect and may require review."
            )

# ---------------- FLASHCARDS (UNCHANGED) ----------------

with tabs[2]:
    st.subheader("Revision Flashcards")

    if not st.session_state.cards:
        st.info(
            "Generate a quiz first to create flashcards."
        )
    else:
        cards = st.session_state.cards

        for index, card in enumerate(cards, 1):
            with st.expander(
                f"Flashcard {index}: {card['question']}"
            ):
                st.markdown(
                    f"**Answer:** {card['answer']}"
                )

                st.markdown("**Explanation:**")
                st.write(card["explanation"])

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)

        writer.writerow([
            "Question", "Answer", "Explanation"
        ])

        for card in cards:
            writer.writerow([
                card["question"],
                card["answer"],
                card["explanation"]
            ])

        st.download_button(
            "⬇️ Download Flashcards (CSV)",
            csv_buffer.getvalue(),
            file_name="ScholarAI_Flashcards.csv",
            mime="text/csv"
        )

    st.caption(
        "Flashcards are generated by AI; numerical "
        "answers should be checked independently."
    )

# ---------------- PROGRESS (UNCHANGED) ----------------

with tabs[3]:
    st.subheader("Your Study Dashboard")

    data = get_history()

    if data.empty:
        st.info(
            "Complete a quiz to begin tracking progress."
        )

    else:
        a, b, c = st.columns(3)

        a.metric("Quizzes", len(data))

        b.metric(
            "Average Score",
            f"{data['percentage'].mean():.1f}%"
        )

        c.metric(
            "Highest Score",
            f"{data['percentage'].max():.1f}%"
        )

        st.markdown("#### Performance History")

        graph = data.copy()
        graph["Attempt"] = range(1, len(graph) + 1)

        st.line_chart(
            graph.set_index("Attempt")["percentage"]
        )

        st.markdown("#### Quiz Records")

        st.dataframe(
            data,
            use_container_width=True,
            hide_index=True
        )

        st.download_button(
            "⬇️ Download Progress CSV",
            data.to_csv(index=False),
            file_name="ScholarAI_Progress.csv",
            mime="text/csv"
        )

st.divider()

st.caption(
    "ScholarAI • Python • Streamlit • Gemini • "
    "Ollama • SQLite"
)

st.caption(
    "An educational AI prototype. Generated responses "
    "can be inaccurate. Verify academic content."
)
