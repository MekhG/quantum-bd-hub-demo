"""Word pools for the demo's fictional world.

Institutions combine invented prefixes with ordinary Indian institution words, so the names read naturally but belong
to no real college. People's names are common Indian first and last names combined at random. Emails sit on the
reserved .example domain and phone numbers start with 0 after +91 (no Indian mobile does), so nothing here can
reach a real person.
"""

from __future__ import annotations

# Invented place-like prefixes (checked to avoid well-known real institution names).
PREFIXES = (
    "Aranyaka", "Vihanga", "Suryodaya", "Navdeep", "Prakriti", "Tarangini", "Meghavarna", "Shilpanagar",
    "Kalpavriksha", "Neelgiri Vale", "Ambarvalley", "Swarnarekha", "Chandrakanta", "Devgiri Heights", "Kaushambi Ridge",
    "Pushpagiri", "Hemavati", "Indradhanu", "Jyotirmay", "Vasundhara", "Sahasrara", "Mayurakshi", "Ratnagiri Coast",
    "Shantiniketan Hills", "Vrindavanam", "Akashganga", "Trivenika", "Dhanushkoti", "Harithavanam", "Saptaparni",
    "Kamalpur", "Rajhans", "Basantpur", "Sitapuram", "Aravalli Crest", "Kanchanpur", "Nandanvan", "Tulasipur",
    "Ujjwala", "Prabhat Nagar", "Sudarshan", "Vidyanagari", "Anandavan", "Mohanpur", "Kesarpur", "Shivalik Vale",
    "Gangotri Plains", "Sonapur", "Chandanpur", "Malayagiri", "Rohini Vale", "Amaravana", "Ketaki", "Manasarovar",
    "Bhuvanagiri", "Sharada Peeth", "Tamraparni", "Padmavati", "Sindhudurg Coast", "Vanasthali Ridge",
)

UNIVERSITY_SUFFIXES = ("University", "Technological University", "University of Science and Technology",
                       "Institute of Technology (Deemed University)", "State University")
COLLEGE_SUFFIXES = ("College of Engineering", "Institute of Technology", "Engineering College",
                    "College of Science", "Institute of Engineering and Technology", "Science and Arts College",
                    "Polytechnic College", "Institute of Information Technology")
STANDALONE_SUFFIXES = ("Institute of Management and Technology", "School of Applied Sciences",
                       "Institute of Advanced Studies", "Centre for Technical Education")

# Real states with real cities: institutions are fictional, places are not.
STATES: dict[str, tuple[str, ...]] = {
    "Telangana": ("Hyderabad", "Warangal", "Karimnagar", "Nizamabad", "Khammam"),
    "Andhra Pradesh": ("Visakhapatnam", "Vijayawada", "Guntur", "Tirupati", "Kakinada"),
    "Karnataka": ("Bengaluru", "Mysuru", "Mangaluru", "Hubballi", "Belagavi"),
    "Tamil Nadu": ("Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Salem"),
    "Kerala": ("Thiruvananthapuram", "Kochi", "Kozhikode", "Thrissur"),
    "Maharashtra": ("Pune", "Mumbai", "Nagpur", "Nashik", "Aurangabad"),
    "Gujarat": ("Ahmedabad", "Vadodara", "Surat", "Rajkot"),
    "Rajasthan": ("Jaipur", "Jodhpur", "Udaipur", "Kota"),
    "Uttar Pradesh": ("Lucknow", "Kanpur", "Noida", "Varanasi", "Prayagraj"),
    "Madhya Pradesh": ("Bhopal", "Indore", "Jabalpur", "Gwalior"),
    "West Bengal": ("Kolkata", "Durgapur", "Siliguri", "Kharagpur"),
    "Odisha": ("Bhubaneswar", "Cuttack", "Rourkela", "Berhampur"),
    "Punjab": ("Mohali", "Ludhiana", "Amritsar", "Jalandhar"),
    "Haryana": ("Gurugram", "Faridabad", "Kurukshetra", "Hisar"),
    "Delhi": ("New Delhi",),
    "Assam": ("Guwahati", "Dibrugarh", "Silchar"),
    "Bihar": ("Patna", "Gaya", "Bhagalpur"),
    "Jharkhand": ("Ranchi", "Jamshedpur", "Dhanbad"),
    "Uttarakhand": ("Dehradun", "Haridwar", "Roorkee"),
    "Himachal Pradesh": ("Shimla", "Mandi", "Solan"),
}
# Heavier weight for the states the team works most (south and west first).
STATE_WEIGHTS = {"Telangana": 9, "Andhra Pradesh": 6, "Karnataka": 8, "Tamil Nadu": 8, "Maharashtra": 8, "Kerala": 4,
                 "Gujarat": 4, "Uttar Pradesh": 5, "West Bengal": 4, "Odisha": 3, "Madhya Pradesh": 3, "Rajasthan": 3,
                 "Punjab": 2, "Haryana": 3, "Delhi": 3, "Assam": 2, "Bihar": 2, "Jharkhand": 2, "Uttarakhand": 2,
                 "Himachal Pradesh": 1}

MANAGEMENT = (("State Government", 30), ("Private Un-Aided", 45), ("Private Aided (Government Aided)", 12),
              ("Central Government", 8), ("Local Body", 5))
COLLEGE_CATEGORIES = ("Affiliated College", "Constituent / University College", "Technical / Polytechnic",
                      "Autonomous College")

FIRST_NAMES = (
    "Aditi", "Ananya", "Arvind", "Bhavna", "Chetan", "Deepa", "Farhan", "Gayatri", "Harish", "Ishita", "Jayant",
    "Kavitha", "Lakshmi", "Manoj", "Meera", "Nikhil", "Pallavi", "Pradeep", "Rahul", "Ramesh", "Rekha", "Sameer",
    "Sangeeta", "Shalini", "Srinivas", "Sunita", "Tarun", "Usha", "Varun", "Vidya", "Yamini", "Zoya", "Gopal",
    "Anil", "Priya", "Suresh", "Kiran", "Madhavi", "Naveen", "Pooja", "Rajesh", "Sneha", "Vikram", "Asha", "Imran",
    "Joseph", "Fatima", "Harpreet", "Debasish", "Sayantani", "Tenzin", "Lalitha", "Venkatesh", "Mohit", "Nandini",
)
LAST_NAMES = (
    "Agarwal", "Bhat", "Chatterjee", "Das", "Deshpande", "Gupta", "Iyer", "Joshi", "Kapoor", "Khan", "Kulkarni",
    "Mehta", "Menon", "Mishra", "Mukherjee", "Nair", "Naidu", "Patel", "Pillai", "Prasad", "Rao", "Reddy", "Saxena",
    "Sen", "Shah", "Sharma", "Singh", "Srinivasan", "Thomas", "Varma", "Verma", "Yadav", "Banerjee", "Ghosh", "Hegde",
    "Kaur", "Krishnan", "Murthy", "Pandey", "Rathore", "Sethi", "Subramanian", "Tiwari", "D'Souza", "Bora",
)

# (contact type, designations, department weightings)
DESIGNATIONS = (
    ("Faculty", ("Assistant Professor", "Associate Professor", "Professor"), 50),
    ("HoD", ("Head of Department", "HoD and Professor"), 22),
    ("Dean", ("Dean (R&D)", "Dean (Academics)"), 8),
    ("Director/Principal", ("Principal", "Director", "Vice Chancellor"), 10),
    ("Registrar", ("Registrar",), 3),
    ("Purchase/Admin", ("Purchase Officer", "Administrative Officer"), 7),
)
DEPARTMENTS = (("Physics", 26), ("ECE", 24), ("CSE", 24), ("EEE", 10), ("Applied Sciences", 8), ("IT", 5),
               ("Mathematics", 3))
RESEARCH = {
    "Physics": ("Quantum optics", "Condensed matter", "Atomic physics", "Photonics", "Spectroscopy"),
    "ECE": ("Optical communication", "Signal processing", "Photonic devices", "Network security"),
    "CSE": ("Quantum computing", "Cryptography", "Machine learning", "Distributed systems"),
    "EEE": ("Power electronics", "Control systems", "Sensors"),
    "Applied Sciences": ("Engineering physics", "Materials science"),
    "IT": ("Cyber security", "Cloud computing"),
    "Mathematics": ("Linear algebra", "Number theory"),
}
INTEREST_AREAS = ("Quantum Computing Curriculum", "Quantum Awareness", "Research Collaboration", "Quantum Hardware",
                  "Faculty Training/Enablement")

# Messages they send us (replies and enquiries) — short, ordinary, made up.
REPLIES = (
    "Thanks for reaching out. Could you share the brochure and indicative pricing?",
    "We are setting up a quantum lab under the new minor programme. Please send details of the simulators.",
    "Interested. Our department would like to see a demo next week.",
    "Please share a proposal for a starter lab; our budget meeting is this month.",
    "Can the simulator run on our existing GPU server? Please send the requirements.",
    "We have funds under the UG lab scheme. Kindly share the quotation format for the kits.",
    "Forwarded to our Dean R&D. Please call me on Thursday afternoon.",
    "Thank you. We already have a lab, but we are interested in the QKD kit for final-year projects.",
)
ENQUIRIES = (
    "Saw your post on LinkedIn — do you offer a student edition of the simulator?",
    "Our college wants to start a quantum computing club. What do you suggest for 40 students?",
    "Please share the price of the QKD experimentation kit and the delivery time.",
    "Can you conduct a two-day workshop for our faculty on quantum computing?",
    "I am a PhD scholar. Is NetSim Mini available for individuals?",
    "We are an engineering college in a tier-2 city. What would a starter lab cost?",
)
CALL_OUTCOMES = ("Spoke to the HoD; asked to resend details", "No answer; left a message", "Busy — call back next week",
                 "Interested; wants a demo for the department", "Spoke briefly; forwarding to the principal",
                 "Asked about pricing and delivery", "Number switched off")
EMAIL_OUTCOMES = ("Sent", "Sent with brochure", "Sent — personalised for the department")
LOST_NOTES = {
    "Budget": "No funds this financial year; revisit after April.",
    "Timing": "Accreditation visit this term; asked us to come back next semester.",
    "No faculty": "No faculty yet to run a quantum lab.",
    "Competitor": "Went with another vendor's simulator.",
    "Procurement block": "Purchase committee wants a GeM listing.",
}
