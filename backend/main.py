import os
import shutil
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    Depends,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
import httpx
import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import auth
from database import Base, engine, get_db
from models.complaint import User, Complaint, Resolution

# --------------------------------------------------------------------------
# LOCAL PYDANTIC SCHEMAS (Self-contained to avoid any schema import errors)
# --------------------------------------------------------------------------

class UserRegisterSchema(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    role: Optional[str] = "citizen"

class ComplaintCreateSchema(BaseModel):
    title: str
    description: str
    category: str
    location: str
    priority: str
    image_url: Optional[str] = None

class StatusUpdateSchema(BaseModel):
    status: str

class ResolutionCreateSchema(BaseModel):
    notes: str
    resolution_image_url: Optional[str] = None


# --------------------------------------------------------------------------
# FASTAPI APP INITIALIZATION
# --------------------------------------------------------------------------

app = FastAPI(title="CivicFix Municipal Redressal Engine", version="1.0.0")

@app.on_event("startup")
async def init_tables():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"Startup DB init notice: {e}")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------------------------------

async def get_current_user(
    token: str = Depends(auth.oauth2_scheme), 
    db: AsyncSession = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except Exception:
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user


@app.get("/")
def health_check():
    return {"status": "healthy", "service": "CivicFix API", "version": "1.0.0"}


@app.post("/auth/register", tags=["Authentication"])
async def register(user_data: UserRegisterSchema, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        full_name=user_data.full_name,
        email=user_data.email,
        hashed_password=auth.get_password_hash(user_data.password),
        role=user_data.role or "citizen"
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {
        "id": new_user.id,
        "full_name": new_user.full_name,
        "email": new_user.email,
        "role": new_user.role
    }


@app.post("/auth/login", tags=["Authentication"])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(), 
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalars().first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expire = datetime.utcnow() + timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user.email, "role": user.role, "exp": expire}
    access_token = jwt.encode(token_data, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/auth/me", tags=["Authentication"])
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "full_name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role
    }


# --------------------------------------------------------------------------
# GRIEVANCE & COMPLAINT ROUTES
# --------------------------------------------------------------------------

@app.get("/departments", tags=["Grievances"])
async def get_departments():
    return [
        {"id": 1, "name": "Roads & Infrastructure"},
        {"id": 2, "name": "Sanitation & Waste"},
        {"id": 3, "name": "Electrical & Lighting"},
        {"id": 4, "name": "Water Supply & Drainage"},
    ]


@app.post("/upload", tags=["Grievances"])
async def upload_media(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1] or ".png"
    unique_name = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join("uploads", unique_name)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    media_type = "video" if ext.lower() in [".mp4", ".mov", ".webm"] else "image"
    return {
        "file_url": f"https://civicfix-t3mx.onrender.com/uploads/{unique_name}",
        "media_type": media_type
    }


@app.post("/complaints", tags=["Grievances"])
async def create_complaint(
    complaint_data: ComplaintCreateSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    ticket_id = f"CF-2026-{uuid.uuid4().hex[:6].upper()}"
    new_complaint = Complaint(
        ticket_id=ticket_id,
        title=complaint_data.title,
        description=complaint_data.description,
        category=complaint_data.category,
        priority=complaint_data.priority,
        location=complaint_data.location,
        image_url=complaint_data.image_url,
        status="Submitted",
        user_id=current_user.id
    )
    db.add(new_complaint)
    await db.commit()
    await db.refresh(new_complaint)
    return new_complaint


@app.get("/complaints", tags=["Grievances", "Public Redressal"])
@app.get("/api/public/complaints", tags=["Public Redressal"])
async def list_public_complaints(
    location: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """Public city-wide complaint feed accessible by citizens, partner services (e.g. BuildWise), and municipal dashboards."""
    stmt = select(Complaint).options(
        selectinload(Complaint.resolution),
        selectinload(Complaint.department)
    )
    if location:
        stmt = stmt.where(Complaint.location.ilike(f"%{location}%"))
    if status and status.lower() != "all":
        stmt = stmt.where(Complaint.status == status)
    if category and category.lower() != "all":
        stmt = stmt.where(Complaint.category == category)
    stmt = stmt.order_by(Complaint.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@app.get("/api/public/complaints/{ticket_id}", tags=["Public Redressal"])
async def get_public_complaint(ticket_id: str, db: AsyncSession = Depends(get_db)):
    """Fetch real-time complaint status and signed resolution notes by ticket ID."""
    stmt = (
        select(Complaint)
        .where(Complaint.ticket_id == ticket_id)
        .options(
            selectinload(Complaint.resolution),
            selectinload(Complaint.department)
        )
    )
    result = await db.execute(stmt)
    complaint = result.scalars().first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint ticket not found")
    return complaint


@app.get("/complaints/my", tags=["Grievances"])
async def get_my_complaints(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Complaint)
        .where(Complaint.user_id == current_user.id)
        .options(
            selectinload(Complaint.resolution),
            selectinload(Complaint.department)
        )
        .order_by(Complaint.created_at.desc())
    )
    return result.scalars().all()


@app.get("/admin/complaints", tags=["Admin Operations"])
async def get_all_complaints(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Complaint)
        .options(
            selectinload(Complaint.resolution),
            selectinload(Complaint.department)
        )
        .order_by(Complaint.created_at.desc())
    )
    return result.scalars().all()


@app.patch("/admin/complaints/{complaint_id}/status", tags=["Admin Operations"])
async def update_status(
    complaint_id: int,
    payload: StatusUpdateSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Complaint).where(Complaint.id == complaint_id))
    complaint = result.scalars().first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    complaint.status = payload.status
    await db.commit()
    await db.refresh(complaint)
    return complaint


@app.post("/admin/complaints/{complaint_id}/resolve", tags=["Admin Operations"])
async def resolve_complaint(
    complaint_id: int,
    payload: ResolutionCreateSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Complaint).where(Complaint.id == complaint_id))
    complaint = result.scalars().first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    complaint.status = "Resolved"
    new_res = Resolution(
        complaint_id=complaint.id,
        notes=payload.notes,
        resolved_by=current_user.full_name,
        resolved_at=datetime.utcnow()
    )
    db.add(new_res)
    await db.commit()
    await db.refresh(complaint)
    return complaint


@app.get("/admin/analytics", tags=["Admin Operations"])
async def get_analytics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    all_complaints = (await db.execute(select(Complaint))).scalars().all()
    total = len(all_complaints)
    pending = sum(1 for c in all_complaints if c.status == "Submitted")
    in_prog = sum(1 for c in all_complaints if c.status == "In Progress")
    resolved = sum(1 for c in all_complaints if c.status == "Resolved")
    return {
        "summary": {
            "total_complaints": total,
            "pending": pending,
            "in_progress": in_prog,
            "resolved": resolved
        }
    }


# =========================================================================
# INTER-SERVICE IMAGE COLLABORATION (CIVICFIX <-> BUILDWISE)
# =========================================================================

BUILDWISE_UPLOAD_URL = os.getenv(
    "BUILDWISE_UPLOAD_URL",
    "https://buildwise-backend-ylo3.onrender.com/api/civic/upload"
).rstrip("/")


# TASK 1: SEND IMAGE FROM CIVICFIX TO BUILDWISE
@app.post("/api/external/send-image", tags=["Inter-Service Image Collaboration"])
async def send_image_to_buildwise(
    file: UploadFile = File(...),
    ticket_id: Optional[str] = Form(None),
    target_url: Optional[str] = Form(None),
):
    dest_url = target_url or BUILDWISE_UPLOAD_URL
    file_bytes = await file.read()

    files = {
        "file": (file.filename, file_bytes, file.content_type or "image/png")
    }
    data = {
        "ticket_id": ticket_id or "CF-2026-LIVE"
    }

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            response = await client.post(dest_url, files=files, data=data)

            if response.status_code in [200, 201]:
                return response.json()

            raise HTTPException(
                status_code=response.status_code,
                detail=f"BuildWise returned status {response.status_code}: {response.text}",
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to communicate with BuildWise server: {str(e)}",
        )


# TASK 2: RECEIVE IMAGE FROM BUILDWISE TO CIVICFIX
@app.post("/api/external/receive-image", tags=["Inter-Service Image Collaboration"])
async def receive_image_from_buildwise(
    file: UploadFile = File(...), 
    ticket_id: Optional[str] = Form(None)
):
    os.makedirs("uploads", exist_ok=True)

    ext = os.path.splitext(file.filename)[1] or ".png"
    unique_name = f"buildwise_{uuid.uuid4().hex[:8]}{ext}"
    dest_path = os.path.join("uploads", unique_name)

    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "status": "success",
        "message": "File received and stored successfully on CivicFix server",
        "filename": unique_name,
        "file_url": f"https://civicfix-t3mx.onrender.com/uploads/{unique_name}",
        "ticket_id": ticket_id or "CF-EXTERNAL",
        "media_type": "image",
    }


# TASK 3: LIST RECEIVED IMAGES ON CIVICFIX
@app.get("/api/external/received-images", tags=["Inter-Service Image Collaboration"])
async def list_received_images():
    """Returns a list of all images received on CivicFix from BuildWise or external collaborators."""
    if not os.path.exists("uploads"):
        return {"images": [], "count": 0}

    files = []
    for fname in sorted(os.listdir("uploads"), reverse=True):
        if fname.startswith("."):
            continue
        fpath = os.path.join("uploads", fname)
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            ext = os.path.splitext(fname)[1].lower()
            files.append({
                "filename": fname,
                "file_url": f"https://civicfix-t3mx.onrender.com/uploads/{fname}",
                "size_bytes": stat.st_size,
                "media_type": "video" if ext in {".mp4", ".mov", ".webm"} else "image",
                "created_at": stat.st_mtime,
            })
    return {"images": files, "count": len(files)}