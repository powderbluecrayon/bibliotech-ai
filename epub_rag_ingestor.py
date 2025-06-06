import os
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from langchain.text_splitter import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_batch
import logging
from tqdm import tqdm

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EPUBProcessor:
    def __init__(self, input_dir, db_config, chunk_size=512, chunk_overlap=64):
        """
        Initialize the EPUB processor
        
        Args:
            input_dir: Directory containing EPUB files
            db_config: Dictionary with database connection parameters
        """
        self.input_dir = input_dir
        self.db_config = db_config
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        
        # Initialize text splitter and embedding model
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
        )
        self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        
        # Connect to PostgreSQL
        self.conn = self._connect_to_db()
        self._setup_database()

    def _connect_to_db(self):
        """Establish a connection to PostgreSQL database"""
        try:
            conn = psycopg2.connect(
                dbname=self.db_config['dbname'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                host=self.db_config['host'],
                port=self.db_config['port']
            )
            logger.info("Successfully connected to PostgreSQL database")
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    def _setup_database(self):
        """Create necessary tables if they don't exist"""
        try:
            with self.conn.cursor() as cursor:
                # Enable pgvector extension if not already enabled
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                
                # Create table for storing chunks and embeddings
                create_table_query = """
                CREATE TABLE IF NOT EXISTS epub_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    book_title TEXT NOT NULL,
                    chunk_num INTEGER NOT NULL,
                    embedding VECTOR(384) NOT NULL
                )
                """
                cursor.execute(create_table_query)
                self.conn.commit()
                logger.info("Database schema initialized")
        except Exception as e:
            logger.error(f"Database setup failed: {e}")
            self.conn.rollback()
            raise

    def extract_text_from_epub(self, epub_path):
        """Extract text content from an EPUB file"""
        try:
            book = epub.read_epub(epub_path)
            chapters = []
            
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), 'html.parser')
                    text = soup.get_text()
                    if text.strip():
                        chapters.append(text)
            
            return ' '.join(chapters)
        except Exception as e:
            logger.error(f"Failed to extract text from {epub_path}: {e}")
            return ""

    def process_directory(self):
        """Process all EPUB files in the input directory and store in database"""
        epub_files = [f for f in os.listdir(self.input_dir) if f.endswith('.epub')]
        all_records = []
        
        for epub_file in tqdm(epub_files, desc="Processing EPUBs"):
            epub_path = os.path.join(self.input_dir, epub_file)
            book_title = os.path.splitext(epub_file)[0]
            
            # Extract text from EPUB
            full_text = self.extract_text_from_epub(epub_path)
            if not full_text:
                continue
                
            # Split text into chunks
            chunks = self.text_splitter.split_text(full_text)
            
            # Generate embeddings for all chunks
            embeddings = self.embedding_model.encode(chunks)
            
            # Prepare records for database insertion
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                record = {
                    'chunk_id': f"{book_title}_chunk_{i}",
                    'content': chunk,
                    'book_title': book_title,
                    'chunk_num': i,
                    'embedding': embedding.tolist()  # Convert numpy array to list
                }
                all_records.append(record)
        
        # Save to PostgreSQL
        self._save_to_postgres(all_records)
        logger.info(f"Processed {len(all_records)} chunks from {len(epub_files)} EPUBs")
        return len(all_records)

    def _save_to_postgres(self, records):
        """Save processed chunks and embeddings to PostgreSQL"""
        try:
            with self.conn.cursor() as cursor:
                # Prepare batch insert
                insert_query = sql.SQL("""
                    INSERT INTO epub_chunks 
                    (chunk_id, content, book_title, chunk_num, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        content = EXCLUDED.content,
                        book_title = EXCLUDED.book_title,
                        chunk_num = EXCLUDED.chunk_num,
                        embedding = EXCLUDED.embedding
                """)
                
                # Execute batch insert
                data = [
                    (
                        record['chunk_id'],
                        record['content'],
                        record['book_title'],
                        record['chunk_num'],
                        record['embedding']
                    )
                    for record in records
                ]
                
                execute_batch(cursor, insert_query, data, page_size=100)
                self.conn.commit()
                logger.info(f"Successfully saved {len(data)} records to database")
        except Exception as e:
            logger.error(f"Failed to save to database: {e}")
            self.conn.rollback()
            raise

    def __del__(self):
        """Close database connection when object is destroyed"""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()
            logger.info("Database connection closed")

if __name__ == "__main__":
    # Example configuration - should be set via environment variables in production
    db_config = {
        'dbname': os.getenv('DB_NAME', 'rag_db'),
        'user': os.getenv('DB_USER', 'postgres'),
        'password': os.getenv('DB_PASSWORD', 'postgres'),
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': os.getenv('DB_PORT', '5432')
    }
    
    input_dir = "path/to/epubs"
    
    try:
        processor = EPUBProcessor(input_dir, db_config)
        total_chunks = processor.process_directory()
        print(f"Successfully processed and stored {total_chunks} chunks in PostgreSQL")
    except Exception as e:
        print(f"An error occurred: {e}")
