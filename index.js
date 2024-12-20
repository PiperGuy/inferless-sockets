const express = require('express');
const app = express();
const PORT = 4000;

//New imports
const http = require('http').Server(app);
const cors = require('cors');
app.use(express.json());

app.use(cors());

app.get('/api/ping', (req, res) => {
	res.json({
		message: 'pong'
	});
});

app.get('/', (req, res) => res.send('inferless socket server'));

const allowedOrigins = ['http://localhost:3000', 'https://console-dev.inferless.com'];

const socketIO = require('socket.io')(http, {
	cors: {
		origin: (origin, callback) => {
			if (!origin || allowedOrigins.includes(origin)) {
				callback(null, true); // Allow the connection
			} else {
				callback(new Error('CORS policy violation')); // Block the connection
			}
		},
		methods: ['GET', 'POST'], // Allowed methods
		credentials: true // If credentials are required
	}
});

socketIO.use((socket, next) => {
	// console.log({ headers: socket });
	// const token = socket.handshake.headers.authorization?.split(' ')[1];
	// console.log(token);
	// if (!token) {
	// 	return next(new Error('Authentication error'));
	// }
	try {
		// const decoded = jwt.verify(token, 'your-secret-key'); // Replace 'your-secret-key' with your JWT secret
		// socket.user = decoded; // Store user info in socket instance
		next();
	} catch (err) {
		next(new Error('Authentication error'));
	}
});

//Add this before the app.get() block

const connectedClients = {};

socketIO.on('connection', (socket) => {
	socket.on('register', (data) => {
		const userId = data.userId;
		if (userId) {
			connectedClients[userId] = socket;
			console.log(`⚡: ${userId} user just connected! `);
		}
	});

	socket.on('message', (data) => {
		console.log(data);
	});
	socket.on('disconnect', () => {
		const userId = Object.keys(connectedClients).find((key) => connectedClients[key] === socket);
		if (userId) {
			delete connectedClients[userId];
			console.log(`User disconnected: ${userId}`);
		}
	});
});

app.post('/api/send_events', (req, res) => {
	const { userId, event } = req.body;

	if (!userId || !event) {
		return res.status(400).json({ message: 'UserId and event are required' });
	}

	const userSocket = connectedClients[userId];
	if (userSocket) {
		userSocket.emit('events', event);
		return res.json({ message: 'Event sent' });
	}

	res.status(404).json({ message: 'User not connected' });
});

http.listen(PORT, () => {
	console.log(`Server listening on ${PORT}`);
});
