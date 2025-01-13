import chainlit as cl
from dotenv import load_dotenv
import logging
from io import BytesIO
from chainlit.element import Audio
from openai import AsyncOpenAI
from plotly.graph_objs import Figure
import os
from utils import generate_sqlite_table_info_query
from tools import tools_schema, run_sqlite_query, Chart_Agent
from bot import ChatBot
from chainlit import AudioChunk

# Load environment variables from .env file
load_dotenv("../.env")

# Configure logging
logging.basicConfig(filename='chatbot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()
logger.addHandler(logging.FileHandler('chatbot.log'))

MAX_ITER = 5
schema_table_pairs = []

tool_run_sqlite_query = cl.step(type="tool", show_input="json", language="str")(run_sqlite_query)
tool_plot_chart = cl.step(type="tool", show_input="json", language="json")(Chart_Agent)
original_run_sqlite_query = tool_run_sqlite_query.__wrapped__

# Set up the transcription API (e.g., Eleven Labs)
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")

if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
    raise ValueError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be set")

# Set up the OpenAI API
client = AsyncOpenAI()

@cl.step(type="tool")
async def speech_to_text(audio_file):
    response = await client.audio.transcriptions.create(
        model="whisper-1", file=audio_file
    )
    return response.text

@cl.on_chat_start
async def on_chat_start():
    # Configure Chainlit features for audio capture
    cl.user_session.set("audio_settings", {
        "min_decibels": -20,
        "initial_silence_timeout": 2000,
        "silence_timeout": 3500,
        "max_duration": 15000,
        "chunk_duration": 1000,
        "sample_rate": 44100
    })
    print("Chat session started and audio settings configured")
    
    # Initialize audio buffer and mime type
    cl.user_session.set("audio_buffer", None)
    cl.user_session.set("audio_mime_type", None)
    
    # Build schema query
    table_info_query = generate_sqlite_table_info_query(schema_table_pairs)

    # Execute query
    result, column_names = await original_run_sqlite_query(table_info_query, markdown=False)

    # Format result into string to be used in prompt
    table_info = '\n'.join([item[0] for item in result])

    system_message = f"""You are an expert in data analysis, an assistant to Business users, owner and other non-technical users. You will provide valuable insights for business users based on their requests.
    Before responding, you will ensure that the user's question pertains to data analysis on the provided schema, otherwise decline.
    If the user requests data, you will build an SQL query based on the user request for the SQLite database from the provided schema/table details and call query_db tools to fetch data from the database with the correct/relevant query that gives accurate results.
    You have access to tools to execute database queries, get results, and plot the query results.
    Once you have provided the data, you will reflect to see if you have provided correct data or not, as you don't know the data beforehand but only the schema, so you might discover new insights while reflecting.

    Follow these Guidelines:
    - If you need certain inputs to proceed or are unsure about anything, you may ask questions, but try to use your intelligence to understand user intention and also let the user know if you make assumptions.
    - In the response message, do not provide technical details like SQL, table, or column details; the response will be read by a business user, not a technical person.
    - Provide rich markdown responses - if it is table data, show it in markdown table format.
    - In case you get a database error, reflect and try to call the correct SQL query.
    - Limit top N queries to 5 and let the user know that you have limited results.
    - Limit the number of columns to 5-8. Wisely choose top columns to query in SQL queries based on the user request.
    - When users ask for all records, limit results to 10 and tell them that you are limiting records.
    - In SQL queries to fetch data, cast date and numeric columns into a readable form (easy to read in string format).
    - Design robust SQL queries that take care of uppercase, lowercase, or some variations because you don't know the complete data or list of enumerable values in columns.
    - Pay careful attention to the schema and table details provided below. Only use columns and tables mentioned in the schema details.

    Here are complete schema details with column details:
    {table_info}"""

    tool_functions = {
        "query_db": tool_run_sqlite_query,
        "plot_chart": tool_plot_chart
    }

    cl.user_session.set("bot", ChatBot(system_message, tools_schema, tool_functions))

@cl.on_message
async def on_message(message: cl.Message):
    # Add author name to the message
    message.author = "DataU"  # Set the author name
    await process_message(message.content)

@cl.on_audio_chunk
async def on_audio_chunk(chunk: AudioChunk):
    print("Received audio chunk")
    try:
        if chunk.isStart:
            buffer = BytesIO()
            buffer.name = f"input_audio.{chunk.mimeType.split('/')[1]}"
            # Initialize the session for a new audio stream
            cl.user_session.set("audio_buffer", buffer)
            cl.user_session.set("audio_mime_type", chunk.mimeType)

        audio_buffer = cl.user_session.get("audio_buffer")
        if audio_buffer is not None:
            audio_buffer.write(chunk.data)
        else:
            print("Audio buffer is not initialized.")
    
    except Exception as e:
        print(f"Error handling audio chunk: {e}")

@cl.on_audio_end
async def on_audio_end(elements: list[Audio]):
    try:
        print("Audio recording ended")
        audio_buffer: BytesIO = cl.user_session.get("audio_buffer")
        if audio_buffer is None:
            print("No audio buffer found.")
            await cl.Message(content="No audio recorded. Please try again.").send()
            return
        audio_buffer.seek(0)
        audio_file = audio_buffer.read()
        audio_mime_type: str = cl.user_session.get("audio_mime_type")

        input_audio_el = Audio(
            mime=audio_mime_type, content=audio_file, name=audio_buffer.name
        )
        await cl.Message(
            author="You",
            type="user_message",
            content="",
            elements=[input_audio_el, *elements]
        ).send()

        whisper_input = (audio_buffer.name, audio_file, audio_mime_type)
        transcription = await speech_to_text(whisper_input)
        print("Transcription received:", transcription)

        await process_message(transcription)

    except Exception as e:
        print(f"Error processing audio: {e}")
        await cl.Message(content="Error processing audio. Please try again.").send()

    finally:
        # Reset audio buffer and mime type
        cl.user_session.set("audio_buffer", None)
        cl.user_session.set("audio_mime_type", None)
        print("Audio buffer reset")

async def process_message(message_content: str):
    bot = cl.user_session.get("bot")  # Retrieve the bot instance from the user session
    if bot is None:
        print("Bot instance is not initialized.")
        return  # Exit the function if bot is not available

    # Create a message object for the Assistant
    msg = cl.Message(author="DataU", content="")
    await msg.send()  # Send an empty message to indicate processing has started

    try:
        # Step 1: Get the response from the bot based on the user's message
        response_message = await bot(message_content)
    except Exception as e:
        print(f"Error calling bot: {e}")
        return  # Exit the function on error

    msg.content = response_message.content or ""

    # Step 2: Send the response back to the user if there's content
    if len(msg.content) > 0:
        await msg.update()

    # Step 3: Handle tool calls if any
    cur_iter = 0
    tool_calls = response_message.tool_calls
    while cur_iter <= MAX_ITER:
        if tool_calls:
            bot.messages.append(response_message)  # Add the response to the bot's message history
            response_message, function_responses = await bot.call_functions(tool_calls)

            # Send the response back to the user
            if response_message.content and len(response_message.content) > 0:
                await cl.Message(author="Assistant", content=response_message.content).send()

            # Reassign tool_calls from the new response
            tool_calls = response_message.tool_calls

            # Handle any function responses that need to be displayed
            function_responses_to_display = [res for res in function_responses if res['name'] in bot.exclude_functions]
            for function_res in function_responses_to_display:
                if isinstance(function_res["content"], Figure):
                    chart = cl.Plotly(name="chart", figure=function_res['content'], display="inline")
                    await cl.Message(author="Assistant", content="", elements=[chart]).send()
        else:
            break
        cur_iter += 1