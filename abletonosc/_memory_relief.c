#include <Python.h>
#include <malloc/malloc.h>

static PyObject* pressure_relief(PyObject* self, PyObject* args) {
    size_t freed = malloc_zone_pressure_relief(NULL, 0);
    return PyLong_FromSize_t(freed);
}

static PyMethodDef methods[] = {
    {"pressure_relief", pressure_relief, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_memory_relief", NULL, -1, methods
};

PyMODINIT_FUNC PyInit__memory_relief(void) {
    return PyModule_Create(&module);
}
