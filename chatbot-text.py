import os
import json
import chromadb
import argparse
from chromadb.utils import embedding_functions
from tqdm import tqdm
from google import genai
import os
import subprocess
import openai
import sys
from dotenv import load_dotenv
import re

# Load environment variables (for API keys)
load_dotenv()

class PdfChatbot:
    def __init__(self, content_dir=None, model_name="gemini-pro", use_chroma=True):
        """
        Initialize the chatbot with a given content directory
        
        Args:
            content_dir: Directory containing extracted PDF content
            model_name: Name of the Gemini model to use
            use_chroma: Whether to use ChromaDB for vector storage
        """
        self.content_dir = self._find_content_dir(content_dir)
        self.chunks = []
        self.use_chroma = use_chroma
        self.chat_history = []
        self.model_name = model_name
        self.use_query_expansion = True  # Control whether to use query expansion
        
        # Setup Gemini API
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables. "
                            "Please add it to a .env file or set it in your environment.")
        
        # Initialize Gemini client
        self.client = genai.Client(api_key=api_key)
        print(f"Initialized Gemini client for model: {model_name}")
        
        # Initialize chat session
        self.current_context = None
        self.chat_session = None
        self.initialize_chat_session()
        
        # Setup ChromaDB
        self._setup_chromadb()
        
        # Load chunks
        self.load_chunks()
        
        print(f"Chatbot initialized with {len(self.chunks)} chunks ready for context")
    
    def initialize_chat_session(self):
        """Create a new chat session with the model and initialize it with system prompt"""
        system_prompt = (
            "You are a helpful assistant specialized in answering questions about a cupra Tavascan manual. "
            "Base your answers only on the provided document context. "
            "If the context doesn't contain enough information to answer, say you don't have enough information. "
            "Always cite the page numbers from the context you used. "
            "Try to be concise and avoid unnecessary details. "
            "You must answer in a json format in the style '{'answer': '...', 'page_numbers': [1, 2, 3], figure_numbers: [1, 2, 3]}' "
            "If there are Figures relevant to the answer, put them in a list"
            "When asked to summarize or explain something from previous context, refer back to that content."
        )
        
        self.chat_session = self.client.chats.create(model=self.model_name)
        # Initialize with system prompt
        self.chat_session.send_message(system_prompt)
        self.current_context = None
    
    def create_new_chat_session(self):
        """Reset the chat session and start fresh"""
        self.initialize_chat_session()
        return self.chat_session
    
    def _find_content_dir(self, content_dir=None):
        """Find content directory if not specified"""
        if content_dir and os.path.exists(content_dir):
            return content_dir
            
        # Look for directories that match the pattern "extracted_content_*"
        current_dir = os.path.dirname(os.path.abspath(__file__))
        potential_dirs = []
        
        for item in os.listdir(current_dir):
            if os.path.isdir(os.path.join(current_dir, item)) and item.startswith("extracted_content_"):
                potential_dirs.append(os.path.join(current_dir, item))
                
        if not potential_dirs:
            raise FileNotFoundError("No extracted content directories found. Please process a PDF first.")
            
        if len(potential_dirs) == 1:
            print(f"Using content directory: {potential_dirs[0]}")
            return potential_dirs[0]
            
        # If multiple directories, ask the user to select one
        print("Multiple content directories found. Please select one:")
        for i, directory in enumerate(potential_dirs):
            print(f"[{i+1}] {os.path.basename(directory)}")
            
        selection = None
        while selection is None:
            try:
                idx = int(input("Enter number: ")) - 1
                if 0 <= idx < len(potential_dirs):
                    selection = potential_dirs[idx]
                else:
                    print("Invalid selection. Try again.")
            except ValueError:
                print("Please enter a number.")
                
        print(f"Using content directory: {selection}")
        return selection
    
    def _setup_chromadb(self):
        """Set up the ChromaDB connection"""
        if self.use_chroma:
            # Use persistent client with path to the chroma_db directory
            chroma_dir = os.path.join(self.content_dir, "chroma_db")
            
            if not os.path.exists(chroma_dir):
                raise FileNotFoundError(
                    f"ChromaDB directory not found at {chroma_dir}. "
                    "Please run retrieval.py first to create the vector database."
                )
                
            # Initialize persistent client
            self.chroma_client = chromadb.PersistentClient(path=chroma_dir)
            sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
            
            # Create or get collection
            collection_name = f"pdf_chunks_{os.path.basename(self.content_dir)}"
            try:
                self.collection = self.chroma_client.get_collection(
                    name=collection_name,
                    embedding_function=sentence_transformer_ef
                )
                print(f"Connected to existing persistent ChromaDB collection: {collection_name}")
            except Exception as e:
                raise ValueError(
                    f"ChromaDB collection '{collection_name}' not found or could not be accessed. "
                    f"Error: {str(e)}. "
                    "Please run retrieval.py first to create the collection."
                )
    
    def load_chunks(self):
        """Load the chunks from the JSON file"""
        chunks_path = os.path.join(self.content_dir, "rag_chunks.json")
        
        if not os.path.exists(chunks_path):
            raise FileNotFoundError(f"Chunks file not found at {chunks_path}")
            
        with open(chunks_path, "r", encoding="utf-8") as f:
            self.chunks = json.load(f)
            
        print(f"Loaded {len(self.chunks)} chunks from {chunks_path}")


    def _rerank_chunks(self, chunks, query, top_k=3):
                """
                Re-rank chunks based on relevance to the query
                
                Args:
                    chunks: List of chunks to re-rank
                    query: User query
                    top_k: Number of chunks to return
                    
                Returns:
                    List of most relevant chunks
                """
                # Calculate relevance scores using simple term matching
                query_terms = set(query.lower().split())
                
                for chunk in chunks:
                    # Count term overlap between query and chunk text
                    chunk_text = chunk.get('text', '').lower()
                    matched_terms = sum(1 for term in query_terms if term in chunk_text)
                    
                    # Simple scoring: term frequency + section title match bonus
                    score = matched_terms
                    
                    # Add bonus for section title matches
                    section_title = chunk.get('section_title', '').lower()
                    section_match = sum(1 for term in query_terms if term in section_title)
                    score += section_match * 2  # Title matches get double weight
                    
                    # Store score in chunk
                    chunk['score'] = score
                
                # Sort by score (descending) and return top_k
                ranked_chunks = sorted(chunks, key=lambda x: x.get('score', 0), reverse=True)
                return ranked_chunks[:top_k]
    
    def expand_query(self, query):
        """
        Expand the query with synonyms and related terms
        
        Args:
            query: The original user query
            
        Returns:
            Expanded query with additional related terms
        """
        # If query expansion is disabled, return the original query
        if not self.use_query_expansion:
            return query
            
        try:
            # Use a separate generation model for query expansion
            expansion_prompt = f"""
            I need to expand this search query to improve retrieval results: "{query}"
            
            Please generate:
            1. 2-3 synonymous ways to phrase the same query
            2. 3-4 related keywords that could help in document retrieval
            3. Any potentially disambiguating terms if the query is unclear
            
            Format the output as a single line with all terms separated by spaces.
            Only include relevant terms, no explanations or other text.
            """
            
            # Generate expanded terms
            response = self.client.generate_content(expansion_prompt)
            expanded_terms = response.text.strip()
            
            # Combine original query with expanded terms
            expanded_query = f"{query} {expanded_terms}"
            
            print(f"Original query: {query}")
            print(f"Expanded query: {expanded_query}")
            
            return expanded_query
            
        except Exception as e:
            print(f"Query expansion error: {str(e)}")
            # Fall back to original query if expansion fails
            return query
    
    def retrieve_context(self, query, top_k=3):
        """
        Retrieve relevant context based on the query
        
        Args:
            query: User query
            top_k: Number of chunks to retrieve
            
        Returns:
            List of context chunks
        """
        if self.use_chroma:
            # Expand the query to improve retrieval
            expanded_query = self.expand_query(query)
            
            # Use ChromaDB for search with the expanded query
            results = self.collection.query(
                query_texts=[expanded_query],
                n_results=top_k * 2  # Retrieve more results initially as we'll filter them later
            )
            
            # Format results to match the original format
            contexts = []
            for i, doc_id in enumerate(results["ids"][0]):
                # Find original chunk
                for chunk in self.chunks:
                    if str(chunk["id"]) == doc_id:
                        contexts.append({
                            "text": chunk["text"],
                            "section_title": chunk["section_title"],
                            "start_page": chunk["start_page"],
                            "score": float(results["distances"][0][i]) if "distances" in results else 0.0
                        })
                        break

            # Extract the start pages of the top chunks
            top_pages = [ctx["start_page"] for ctx in contexts[:5]]

            # Collect all chunks from the top pages
            all_chunks_from_pages = [
                chunk for chunk in self.chunks if chunk["start_page"] in top_pages
            ]

            # Re-rank with the original query to ensure relevance
            reranked = self._rerank_chunks(all_chunks_from_pages, query, top_k=top_k)

            # Check if query refers to specific pages like "page 5" or "p. 10"
            page_references = re.findall(r'page\s+(\d+)|p\.\s*(\d+)', query, re.IGNORECASE)
            referenced_pages = []

            # Extract referenced page numbers
            for page_ref in page_references:
                # Each match is a tuple with groups, only one will have content
                page_num = next((int(p) for p in page_ref if p), None)
                if page_num:
                    referenced_pages.append(page_num)

            # Add chunks from explicitly referenced pages if they're not already included
            # Track if we added new pages
            new_pages_added = False
            for page_num in referenced_pages:
                if page_num not in top_pages:
                    # Add chunks from this page to all_chunks_from_pages
                    page_chunks = [chunk for chunk in self.chunks if chunk["start_page"] == page_num]
                    all_chunks_from_pages.extend(page_chunks)
                    top_pages.append(page_num)  # Add to top_pages to track inclusion
                    new_pages_added = True
            
            # Rerank again if new pages were added
            if new_pages_added:
                reranked = self._rerank_chunks(all_chunks_from_pages, query, top_k=top_k)

            return reranked[:top_k]  # Return only the top_k most relevant chunks
        else:
            raise ValueError("ChromaDB is required for context retrieval")
    
    def format_context_for_prompt(self, contexts):
        """Format retrieved contexts for the prompt"""
        context_text = ""
        
        for i, ctx in enumerate(contexts):
            context_text += f"[Context {i+1} - Page {ctx.get('start_page', 'unknown')} - {ctx.get('section_title', 'Section')}]\n"
            context_text += f"{ctx['text']}\n\n"

        print(f"Formatted context for prompt:\n{context_text}")
            
        return context_text.strip()
    
    def get_response(self, query, top_k=3):
        """
        Get response from Gemini with relevant context
        
        Args:
            query: User query
            top_k: Number of context chunks to use
            
        Returns:
            Generated response
        """
        # Retrieve relevant context based on the query
        contexts = self.retrieve_context(query, top_k=top_k)
        
        if not contexts:
            context_text = "No relevant context found in the document."
        else:
            context_text = self.format_context_for_prompt(contexts)
            # Save the current context for potential follow-up questions
            self.current_context = context_text
        
        # If the query seems like a follow-up question and we have previous context
        if (self.is_followup_question(query) and self.current_context):
            # Use current context from the previous query
            full_prompt = f"Based on this document context:\n{self.current_context}\n\nUser Question: {query}"
        else:
            # Use newly retrieved context
            full_prompt = f"Document Context:\n{context_text}\n\nUser Question: {query}"
        
        # Get response from Gemini using chat session
        try:
            response = self.chat_session.send_message(full_prompt)
            return response.text
        except Exception as e:
            return f"Error generating response: {str(e)}"
    
    def is_followup_question(self, query):
        """Check if the query is likely a follow-up question"""
        followup_indicators = [
            "it", "this", "that", "these", "those", "the", "they", "them", 
            "their", "he", "she", "his", "her", "what about", "how about",
            "can you", "tell me more", "explain", "elaborate", "summarize", 
            "resume", "continue", "go on", "and", "also", "additionally"
        ]
        
        query_lower = query.lower()
        # Check if query is short or contains followup indicators
        return (len(query.split()) <= 5 or 
                any(indicator in query_lower for indicator in followup_indicators))
    
    def display_chat_history(self):
        """Display the current chat history from the model"""
        print("\n" + "="*60)
        print("CHAT HISTORY:")
        print("="*60)
        
        for message in self.chat_session.get_history():
            role = message.role.upper()
            if role == "USER":
                print(f"\n👤 USER: {message.parts[0].text}")
            elif role == "MODEL":
                print(f"\n🤖 ASSISTANT: {message.parts[0].text}")
        print("="*60 + "\n")
    
    def chat(self):
        """Interactive chat interface"""
        print("\n" + "="*60)
        print("PDF Chatbot ready! Ask questions about your document.")
        print("Type 'exit', 'quit', or 'q' to end the conversation.")
        print("Type 'history' to see the conversation history.")
        print("Type 'new' to start a new conversation.")
        print("Type 'expand on/off' to toggle query expansion.")
        print("="*60 + "\n")
        
        while True:
            user_query = input("\nYou: ")
            
            if user_query.lower() in ["exit", "quit", "q"]:
                print("\nGoodbye!")
                break
            
            if user_query.lower() == "history":
                self.display_chat_history()
                continue
                
            if user_query.lower() == "new":
                self.chat_session = self.create_new_chat_session()
                print("\nStarted a new conversation!")
                continue
                
            if user_query.lower() == "expand on":
                self.use_query_expansion = True
                print("\nQuery expansion turned ON")
                continue
                
            if user_query.lower() == "expand off":
                self.use_query_expansion = False
                print("\nQuery expansion turned OFF")
                continue
                
            if not user_query.strip():
                continue
                
            print("\nThinking...")
            response = self.get_response(user_query)

            # Extract JSON from the response
            try:
                response_clean = re.sub(r'```json', '', response)
                response_clean = re.sub(r'```', '', response_clean)
                response_json = json.loads(response_clean)
                print("\nExtracted JSON:")
                print(f"\nAssistant: {json.dumps(response_json, indent=4) }")
              
            except json.JSONDecodeError:
                print("\nError: Response is not a valid JSON.")
            
                print(f"\nAssistant: {response}")
            
            # Add to local history (not needed anymore, but kept for backward compatibility)
            self.chat_history.append({"user": user_query, "assistant": response})

def main():
    """Main function to run the chatbot"""
    parser = argparse.ArgumentParser(description="Chat with your PDF documents using Gemini")
    parser.add_argument("--content_dir", "-d", help="Directory containing the extracted content")
    parser.add_argument("--model", "-m", default="gemini-2.0-flash", help="Gemini model to use")
    parser.add_argument("--top_k", "-k", type=int, default=3, help="Number of context chunks to use")
    args = parser.parse_args()
    
    try:
        # Initialize and start chatbot
        chatbot = PdfChatbot(
            content_dir=args.content_dir,
            model_name=args.model
        )
        chatbot.chat()
    except Exception as e:
        print(f"Error: {e}")
        print("\nTo use this chatbot:")
        print("1. First run pre.py to extract content from your PDF")
        print("2. Then run retrieval.py to create the vector database")
        print("3. Make sure you have a GEMINI_API_KEY in your environment or .env file")
        print("4. Try again!")

if __name__ == "__main__":
    main()
