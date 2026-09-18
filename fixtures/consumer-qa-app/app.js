/* Interacción de la aplicación de referencia de QA Consumer.
 *
 * Todo es determinista y local: sin red, sin relojes, sin aleatoriedad. El botón añade un nodo
 * nuevo (#pong) y el formulario responde con un estado (#estado) que confirma lo enviado. Así una
 * expectativa puede exigir un elemento que **solo existe después** de interactuar.
 */

document.getElementById('ping').addEventListener('click', () => {
  const nodo = document.createElement('span');
  nodo.id = 'pong';
  nodo.textContent = 'pong';
  document.getElementById('salida').appendChild(nodo);
});

document.getElementById('alta').addEventListener('submit', (evento) => {
  evento.preventDefault();
  const estado = document.createElement('p');
  estado.id = 'estado';
  estado.textContent = `recibido ${document.getElementById('nombre').value}`;
  document.getElementById('salida').appendChild(estado);
});
